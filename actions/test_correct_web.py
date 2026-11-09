"""Tests unitaires de l'Action « Corriger sur le web » (logique pure, sans réseau).

Lancement : python test_correctweb.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "correctweb_action.py")

spec = importlib.util.spec_from_file_location("correctweb_action", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILURES = []


def check(name, cond):
    if cond:
        print(f"  OK  {name}")
    else:
        print(f"  ECHEC  {name}")
        FAILURES.append(name)


# ---------------------------------------------------------------- _extract_target
msgs = [
    {"id": "u1", "role": "user", "content": "question"},
    {"id": "a1", "role": "assistant", "content": "réponse 1"},
    {"id": "u2", "role": "user", "content": "suite"},
    {"id": "a2", "role": "assistant", "content": "réponse cible"},
]
mid, content, idx = mod._extract_target({"id": "a1", "messages": msgs})
check("extraction par id cliqué", mid == "a1" and content == "réponse 1" and idx == 1)
mid, content, idx = mod._extract_target({"messages": msgs})
check("extraction repli dernier assistant", mid == "a2" and idx == 3)
check("extraction vide", mod._extract_target({}) == (None, "", -1))

# ---------------------------------------------------------------- _build_prefix
prefix, truncated = mod._build_prefix(msgs, 1, 16000)
check("préfixe coupé à la cible incluse", [m["id"] for m in prefix] == ["u1", "a1"] and not truncated)
prefix, truncated = mod._build_prefix(msgs, 1, 5)
check("troncature contenu cible", truncated and "tronqué" in prefix[-1]["content"])

# ---------------------------------------------------------------- _chain_to_message
history = {
    "currentId": "a3",
    "messages": {
        "u1": {"role": "user", "content": "q1", "parentId": None},
        "a1": {"role": "assistant", "content": "r1", "parentId": "u1"},
        "u2": {"role": "user", "content": "q2", "parentId": "a1"},
        "a2": {"role": "assistant", "content": "r2", "parentId": "u2", "sources": [{"t": 1}]},
        "a3": {"role": "assistant", "content": "r3", "parentId": "u2"},
    },
}
chain = mod._chain_to_message(history, "a2")
check("chaîne racine->cible (leaf ultérieur exclu)", [m["id"] for m in chain] == ["u1", "a1", "u2", "a2"])
check("sources préservées dans la chaîne", chain[-1].get("sources") == [{"t": 1}])
check("chaîne vide si cible inconnue", mod._chain_to_message(history, "zz") == [])

# ---------------------------------------------------------------- _build_instruction


class V:
    language = "auto"
    max_searches = 6
    search_mode = "native"


class VL(V):
    search_mode = "legacy"


instr = mod._build_instruction(V(), True, "extrait du sujet à corriger")
check("budget plafond dur présent", "plafond dur" in instr and "6 appels" in instr)
check("règle d'arrêt présente", "ARRÊT OBLIGATOIRE" in instr)
check("format de balisage exact", "~~séquence erronée~~ **[séquence corrigée](URL \"titre de la source\")**" in instr)
check("interdiction auto-correction (capacités de l'assistant)", "NE corrige JAMAIS" in instr and "ses outils" in instr)
check("interdiction liens pages de résultats", "?q=" in instr)
check("cible = dernière réponse rejouée", "TA DERNIÈRE RÉPONSE" in instr)
check("extrait sujet injecté", "extrait du sujet à corriger" in instr)
check("sortie stricte sans rapport", "Aucun préambule, aucun titre" in instr)
check("aucun format de rapport imposé", "## Vérification factuelle" not in instr and "Verdict global" not in instr)
check("interdiction HTML hors whitelist", "texte brut" in instr)
check("URL uniquement issues de recherches", "réellement retournées" in instr)

instr_legacy = mod._build_instruction(VL(), True, "sujet")
check("mode legacy : pas d'outils annoncés", "Tu DISPOSES d'outils" not in instr_legacy and "PRÉ" not in instr_legacy.split("SUJET")[0].replace("pré-injectées", "X") or "fournis dans le contexte" in instr_legacy)
instr_noweb = mod._build_instruction(V(), False, "sujet")
check("sans web : consigne de non-correction", "ne corrige RIEN" in instr_noweb)

# ---------------------------------------------------------------- _validate_correction
orig = (
    "Le RAM DDR5 est cadencée en moyenne à 4800 MT/s. "
    "Le serveur HPE ProLiant DL380 Gen10 sort en 2017 avec les processeurs Scalable. "
    "Les disques SAS 15k tournent à 10 000 tr/min et la garantie standard dure trois ans."
) * 3

good = orig.replace(
    "avec les processeurs Scalable",
    "~~avec les processeurs Scalable~~ [avec les Xeon Silver 4110](https://example.com/hpe \"Fiche HPE\")",
)
v, n = mod._validate_correction(orig, good)
check("correction valide -> update", v == "update" and n == 3)  # 3 occurrences remplacées

v, _ = mod._validate_correction(orig, orig)
check("verbatim sans marqueur -> unchanged", v == "unchanged")

v, _ = mod._validate_correction(orig, "```\n" + orig + "\n```")
check("verbatim encadré de fences -> unchanged", v == "unchanged")

v, reason = mod._validate_correction(orig, "")
check("vide -> reject", v == "reject" and "vide" in reason)

v, reason = mod._validate_correction(orig, "## Vérification factuelle\nLe texte est correct avec ~~x~~ [y](https://a.fr \"t\").\n" + orig)
check("rapport avec titre -> reject", v == "reject" and "rapport" in reason)

v, reason = mod._validate_correction(orig, "DDR5 à 4800 MT/s, correction ~~2017~~ [2016](https://x.fr \"t\")")
check("résumé -> reject", v == "reject" and "court" in reason)

paraphrase = ("En matière de mémoire, la norme actuelle atteint des fréquences supérieures. "
              "Le matériel du fabricant visé commercialise des machines récentes à composants multiples. "
              "Les supports mécaniques rapides affichent une vélocité moindre que leur appellation. "
              "La couverture contractuelle de base s'étale sur plusieurs années.") * 3
para = paraphrase.replace("plupart", "~~plupart~~ [partout](https://x.fr \"t\")").replace(
    "fréquences", "~~fréquences~~ [cadences](https://y.fr \"t\")")
v, reason = mod._validate_correction(orig, para)
check("paraphrase -> reject", v == "reject" and "paraphrase" in reason)

long_add = good + "\n\nNote : j'ai vérifié chaque point soigneusement, voici le détail complet de mes recherches, " \
    "avec une liste exhaustive de toutes les sources consultées durant cette session de vérification factuelle " \
    "approfondie auprès de multiples moteurs et pages web professionnelles pour garantir la justesse." * 8
v, reason = mod._validate_correction(orig, long_add)
check("texte gonflé de commentaires -> reject", v == "reject" and "long" in reason)

check("fences supprimées", mod._strip_fences("```markdown\nbonjour\n```") == "bonjour")

reworded = orig.replace(
    "Le serveur HPE ProLiant DL380 Gen10 sort en 2017 avec les processeurs Scalable",
    "Le serveur HPE ProLiant DL380 Gen10 est commercialisé à partir de 2017 et embarque les processeurs Scalable Intel",
)
v, detail = mod._validate_correction(orig, reworded)
check("reformulation sans marqueur -> unchanged (pas un faux rejet)", v == "unchanged" and detail == "reformule")

v, detail = mod._validate_correction(orig, "Autre contenu entièrement différent, hors sujet.")
check("sans marqueur et dérive totale -> unchanged/reesquisse (jamais écrit, jamais d'alarme)",
      v == "unchanged" and detail == "reesquisse")

# ---------------------------------------------------------------- _strip_self_corrections
selfmd = ("Voici un point.\n"
          "Je n'ai ~~pas d'outil de recherche web~~ **[des outils de recherche web]"
          "(https://duckduckgo.com/?q=x \"Recherche Web\")** dans cet environnement.\n"
          "Le serveur est sorti en ~~2012~~ **[2017](https://hpe.com \"HPE\")**.")
stripped = mod._strip_self_corrections(selfmd)
check("auto-correction restauree (texte d'origine)", "~~pas d'outil" not in stripped
      and "Je n'ai pas d'outil de recherche web dans cet environnement." in stripped)
check("vraie correction preservee", "~~2012~~ **[2017](https://hpe.com \"HPE\")**" in stripped)
_orig3 = "Voici un point.\nJe n'ai pas d'outil de recherche web dans cet environnement.\nLe serveur est sorti en 2012."
v, n = mod._validate_correction(_orig3, stripped)
check("validate apres sanitizer : 1 seule correction", v == "update" and n == 1)
check(" chevauchement de mots identique", mod._word_overlap(orig, orig) == 1.0)
check("chevauchement de mots divergent", mod._word_overlap(orig, "autre chose totalement différente du tout") < 0.3)

# ---------------------------------------------------------------- _revert_null_replacements (v1.0.6)
nullmd = ('Le serveur ~~DL380~~ **[N/A](https://x.fr "t")** coute '
          '~~3 500 EUR~~ **[MàJ](https://y.fr "t")** et la garantie ~~3 ans~~ **[indisponible](https://z.fr "t")**.')
fixed = mod._revert_null_replacements(nullmd)
check("N/A/MàJ/indisponible annules (mot d origine restaure)",
      fixed == "Le serveur DL380 coute 3 500 EUR et la garantie 3 ans.")
check("vraie valeur conservee",
      mod._revert_null_replacements("sorti en ~~2012~~ **[2017](https://hpe.com \"HPE\")**")
      == "sorti en ~~2012~~ **[2017](https://hpe.com \"HPE\")**")
check("gras vide annule", mod._revert_null_replacements("texte ~~Qwen3.5~~** ") == "texte Qwen3.5")
check("gras pendant fin de ligne annule", mod._revert_null_replacements("fin ~~X~~**\nsuite") == "fin X\nsuite")

# ---------------------------------------------------------------- _arrow_numeric_links (v1.0.7)
check("lien numerique fleche ajoutee",
      mod._arrow_numeric_links('sorti en ~~2012~~ **[2017](https://hpe.com "HPE")**')
      == 'sorti en ~~2012~~ **[→ 2017](https://hpe.com "HPE")**')
check("lien non numerique intact",
      mod._arrow_numeric_links('~~fermé~~ **[poids ouverts](https://x.fr "t")**')
      == '~~fermé~~ **[poids ouverts](https://x.fr "t")**')
check("flèche pas doublée",
      mod._arrow_numeric_links('~~2012~~ **[→ 2017](https://hpe.com "HPE")**')
      == '~~2012~~ **[→ 2017](https://hpe.com "HPE")**')
check("quantite avec espace numerique aussi",
      '[→ 15 000](' in mod._arrow_numeric_links('~~7 200~~ **[15 000](https://x.fr "t")**'))

# ---------------------------------------------------------------- valves
check("valves max_searches défaut 6", mod.Action().Valves.model_fields["max_searches"].default == 6)
check("valves strict_search défaut True", mod.Action().Valves.model_fields["strict_search"].default is True)

if FAILURES:
    print(f"\n{len(FAILURES)} ÉCHEC(S) : {FAILURES}")
    sys.exit(1)
print("\nTOUS LES TESTS PASSENT")
