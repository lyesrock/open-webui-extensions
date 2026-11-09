"""Tests unitaires de l'Action « Corriger sur le web » (logique pure, sans réseau).

Lancement : python test_correctweb.py
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULE_PATH = os.path.join(HERE, "correct_web.py")

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


class VD(V):
    correction_display = "strikethrough"


instr = mod._build_instruction(V(), True, "extrait du sujet à corriger")
check("budget plafond dur présent", "plafond dur" in instr and "6 appels" in instr)
check("règle d'arrêt présente", "ARRÊT OBLIGATOIRE" in instr)
check("v1.1.0 : format marqueur demandé", "**[séquence corrigée](URL \"Corrigé : séquence erronée\")**" in instr)
check("v1.1.0 : suppression du faux exigée, barré interdit", "SUPPRIME TOTALEMENT" in instr and "n'écris jamais ~~…~~" in instr)
check("par défaut pas d'ancien format demandé", "~~séquence erronée~~" not in instr)
instr_strike = mod._build_instruction(VD(), True, "sujet")
check("valve strikethrough : ancien format demandé", "~~séquence erronée~~ **[séquence corrigée](URL \"titre de la source\")**" in instr_strike)
check("valve strikethrough : pas de consigne marqueur", "Corrigé : séquence erronée" not in instr_strike)
check("valve inconnu revient à replace", mod._build_instruction(type("X", (V,), {"correction_display": "nimportequoi"})(), True, "sujet").count("Corrigé : séquence erronée") == 1)
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

# ---------------------------------------------------------------- _resolve_display
check("display défaut replace", mod._resolve_display(V()) == "replace")
check("display valve strikethrough", mod._resolve_display(VD()) == "strikethrough")
check("display casse/espaces tolérée", mod._resolve_display(type("X", (V,), {"correction_display": "  StrikeThrough "})()) == "strikethrough")

# ---------------------------------------------------------------- couleur + citations (v1.1.0/1.1.1)
check("couleur défaut jaune = aucune injection", mod._resolve_color(V()) == "")
check("couleur valve orange", mod._resolve_color(type("X", (V,), {"correction_color": "Orange"})()) == "orange")
check("couleur inconnue retombe sur jaune", mod._resolve_color(type("X", (V,), {"correction_color": "chartreuse"})()) == "")
check("couleur aucune injectée", mod._resolve_color(type("X", (V,), {"correction_color": "aucune"})()) == "aucune")
colored = mod._apply_color('sorti en **[2017](https://hpe.com "Corrigé : 2012")**', "orange")
check("jeton couleur injecté dans le titre",
      colored == 'sorti en **[2017](https://hpe.com "Corrigé #orange : 2012")**')
check("marqueur avec jeton couleur toujours détecté (count)",
      len(mod._CORR_LINK_RE.findall(colored)) == 1)
check("jeton couleur normalisé par convert (re-correction propre)",
      mod._convert_legacy(colored) == 'sorti en **[2017](https://hpe.com "Corrigé : 2012")**')
check("sanitizer fonctionne sur marqueur coloré",
      mod._revert_null_replacements('**[N/A](https://x.fr "Corrigé #vert : 3 ans")**') == "3 ans")
_strip_cit = mod._finalize_render('texte [1][2] corrigé **[x](https://a.fr "Corrigé : y")**[3]',
                                  type("X", (V,), {"show_citations": False})())
check("badges citation retirés quand show_citations=False",
      _strip_cit.startswith('texte corrigé **[x](https://a.fr "Corrigé : y')
      and "[1]" not in _strip_cit and "[2]" not in _strip_cit)
check("lien corrigé numérique Jamais retiré par le strip citation",
      mod._finalize_render('**[ 2021](https://x.fr "Corrigé : 2015")**',
                           type("X", (V,), {"show_citations": False})()).startswith('**[ 2021]'))
check("badges conservés par défaut",
      "texte [1] corrigé" in mod._finalize_render('texte [1] corrigé **[x](https://a.fr "Corrigé : y")**', V()))
fin = mod._finalize_render('sorti en **[ 2017](https://hpe.com "Corrigé : 2012")**',
                           type("X", (V,), {"correction_display": "strikethrough", "correction_color": "rose"})())
check("finalize strikethrough = rendu barré", fin == 'sorti en ~~2012~~ **[ 2017](https://hpe.com)**')
fin2 = mod._finalize_render('sorti en **[ 2017](https://hpe.com "Corrigé : 2012")**',
                            type("X", (V,), {"correction_color": "rose"})())
check("finalize replace + couleur + infobulle barrée",
      fin2 == 'sorti en **[ 2017](https://hpe.com "Corrigé #rose : 2\u03360\u03361\u03362\u0336")**')
check("infobulle barrée idempotente",
      mod._finalize_render(fin2, type("X", (V,), {"correction_color": "rose"})()) == fin2)
check("re-correction d'un titre barré = titre propre",
      mod._convert_legacy(fin2) == 'sorti en **[ 2017](https://hpe.com "Corrigé : 2012")**')

# ---------------------------------------------------------------- _convert_legacy / _render_strikethrough (v1.1.0)
conv = mod._convert_legacy('sorti en ~~2012~~ **[2017](https://hpe.com "HPE")**')
check("legacy -> marqueur canonique (titre fusionné)",
      conv == 'sorti en **[2017](https://hpe.com "Corrigé : 2012 — HPE")**')
conv2 = mod._convert_legacy('sorti en ~~2012~~ **[2017](https://hpe.com)**')
check("legacy sans titre source", conv2 == 'sorti en **[2017](https://hpe.com "Corrigé : 2012")**')
check("titre corrigé minuscule normalisé",
      mod._convert_legacy('**[x](https://a.fr "corrigé : y")**') == '**[x](https://a.fr "Corrigé : y")**')
check("tilde orphelin sans lien -> texte nu", mod._convert_legacy("du ~~vrai~~ faux") == "du vrai faux")
check("gras vide legacy annule", mod._convert_legacy("texte ~~Qwen3.5~~** **\nfin") == "texte Qwen3.5\nfin")
check("gras pendant fin de ligne annule", mod._convert_legacy("fin ~~X~~**\nsuite") == "fin X\nsuite")
check("conversion idempotente", mod._convert_legacy(conv) == conv)
rev = mod._render_strikethrough('sorti en **[2017](https://hpe.com "Corrigé : 2012 — HPE")**')
check("canonique -> rendu barré", rev == 'sorti en ~~2012~~ **[2017](https://hpe.com "HPE")**')
rev2 = mod._render_strikethrough('sorti en **[ 2021](https://x.fr "Corrigé : 2015")**')
check("rendu barré sans source garde l'espace de protection",
      rev2 == 'sorti en ~~2015~~ **[ 2021](https://x.fr)**')
check("rendu barré idempotent sur texte sain", mod._render_strikethrough(conv) == 'sorti en ~~2012~~ **[2017](https://hpe.com "HPE")**')
check("rendu barré sans marqueur = inchangé", mod._render_strikethrough("texte normal") == "texte normal")

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
check("correction legacy auto-convertisse -> update n=3", v == "update" and n == 3)

good_new = orig.replace(
    "dure trois ans",
    'dure **[cinq ans](https://x.fr "Corrigé : trois ans")**',
)
v, n = mod._validate_correction(orig, good_new)
check("correction marqueur -> update n=3", v == "update" and n == 3)

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
para = paraphrase.replace("plupart", '~~plupart~~ [partout](https://x.fr "t")').replace(
    "fréquences", '~~fréquences~~ [cadences](https://y.fr "t")')
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

check("chevauchement de mots identique", mod._word_overlap(orig, orig) == 1.0)
check("chevauchement de mots divergent", mod._word_overlap(orig, "autre chose totalement différente du tout") < 0.3)

# ---------------------------------------------------------------- _strip_self_corrections
selfmd = ("Voici un point.\n"
          "Je n'ai **[des outils de recherche web](https://duckduckgo.com/?q=x "
          "\"Corrigé : pas d'outil de recherche web\")** dans cet environnement.\n"
          "Le serveur est sorti en **[→ 2017](https://hpe.com \"Corrigé : 2012\")**.")
stripped = mod._strip_self_corrections(selfmd)
check("auto-correction restaurée via le titre (texte d'origine)", "Corrigé : pas d'outil" not in stripped
      and "Je n'ai pas d'outil de recherche web dans cet environnement." in stripped)
check("vraie correction préservée", "**[→ 2017](https://hpe.com \"Corrigé : 2012\")**" in stripped)
_orig3 = "Voici un point.\nJe n'ai pas d'outil de recherche web dans cet environnement.\nLe serveur est sorti en 2012."
v, n = mod._validate_correction(_orig3, stripped)
check("validate après sanitizer : 1 seule correction", v == "update" and n == 1)
selfmd_legacy = ("Je n'ai ~~pas d'outil de recherche web~~ **[des outils de recherche web]"
                 "(https://duckduckgo.com/?q=x \"Recherche Web\")** ici.")
chain_self = mod._strip_self_corrections(mod._convert_legacy(selfmd_legacy))
check("chaîne convert+strip sur legacy : auto-correction annulée",
      chain_self == "Je n'ai pas d'outil de recherche web ici.")

# ---------------------------------------------------------------- _revert_null_replacements
nullmd = ('Le serveur **[N/A](https://x.fr "Corrigé : DL380")** coute '
          '**[MàJ](https://y.fr "Corrigé : 3 500 EUR")** et la garantie '
          '**[indisponible](https://z.fr "Corrigé : 3 ans")**.')
fixed = mod._revert_null_replacements(nullmd)
check("N/A/MàJ/indisponible annulés (texte du titre restauré)",
      fixed == "Le serveur DL380 coute 3 500 EUR et la garantie 3 ans.")
check("vraie valeur conservée",
      mod._revert_null_replacements('sorti en **[2017](https://hpe.com "Corrigé : 2012")**')
      == 'sorti en **[2017](https://hpe.com "Corrigé : 2012")**')
check("marqueur de remplacement vide annulé",
      mod._revert_null_replacements('texte **[](https://q.fr "Corrigé : Qwen3.5")**') == "texte Qwen3.5")

# ---------------------------------------------------------------- _protect_numeric_links (espace invisible remplace la flèche)
check("lien numérique espace invisible ajoutée",
      mod._protect_numeric_links('sorti en **[2017](https://hpe.com "Corrigé : 2012")**')
      == 'sorti en **[ 2017](https://hpe.com "Corrigé : 2012")**')
check("lien non numérique intact",
      mod._protect_numeric_links('**[poids ouverts](https://x.fr "Corrigé : fermé")**')
      == '**[poids ouverts](https://x.fr "Corrigé : fermé")**')
check("espace pas doublée",
      mod._protect_numeric_links('**[ 2017](https://hpe.com "Corrigé : 2012")**')
      == '**[ 2017](https://hpe.com "Corrigé : 2012")**')
check("quantité avec espace numérique aussi",
      '[ 15 000](' in mod._protect_numeric_links('**[15 000](https://x.fr "Corrigé : 7 200")**'))
check("flèche héritée v1.0.x consommée",
      mod._protect_numeric_links('**[→ 2017](https://hpe.com "Corrigé : 2012")**')
      == '**[ 2017](https://hpe.com "Corrigé : 2012")**')
check("début de ligne protégé",
      mod._protect_numeric_links('[2012](https://x.fr)') == '[ 2012](https://x.fr)')
check("badge citation sans lien pas touché",
      mod._protect_numeric_links('texte [1] et [2,3] fin') == 'texte [1] et [2,3] fin')
check("flèche legacy retirée par convert",
      mod._convert_legacy('~~2012~~ **[→ 2017](https://hpe.com "HPE")**')
      == '**[2017](https://hpe.com "Corrigé : 2012 — HPE")**')
check("flèche texte non numerique du marqueur retirée",
      mod._strip_marker_arrows('modèle **[→ Qwen 2.5](https://q.fr "Corrigé : Qwen 2")**')
      == 'modèle **[Qwen 2.5](https://q.fr "Corrigé : Qwen 2")**')
check("flèche hors marqueur pas touchée",
      mod._strip_marker_arrows('direction **[→](https://x.fr) puis [→ 2017](https://x.fr)')
      == 'direction **[→](https://x.fr) puis [→ 2017](https://x.fr)')
_chain = mod._strip_self_corrections(mod._revert_null_replacements(
    mod._strip_marker_arrows(mod._protect_numeric_links(mod._convert_legacy(
        'modèle **[→ Qwen 2.5](https://q.fr "Corrigé : Qwen 2")** sorti en **[→ 2017](https://h.fr "HPE")**')))))
check("chaîne complète : plus AUCUNE flèche visible",
      "→" not in _chain and '[ 2017](' in _chain and '[Qwen 2.5](' in _chain)

# ---------------------------------------------------------------- _patch_output_message (v1.0.8)
_out = [
    {"type": "function_call", "name": "search_web"},
    {"type": "function_call_output", "output": "..."},
    {"type": "message", "role": "assistant",
     "content": [{"type": "output_text", "text": "texte d'origine"}]},
]
patched, changed = mod._patch_output_message(_out, '**[→ 2021](https://x.fr "Corrigé : 2015")**')
check("sync timeline remplace le bloc message final", changed)
check("nouveau texte présent",
      patched[-1]["content"][0]["text"] == '**[→ 2021](https://x.fr "Corrigé : 2015")**')
check("blocs outils conservés", patched[0]["type"] == "function_call" and patched[1]["type"] == "function_call_output")
check("output absent = pas de changement", mod._patch_output_message(None, "x") == (None, False))
check("output vide = pas de changement", mod._patch_output_message([], "x") == ([], False))
check("timeline sans bloc message = pas de changement",
      mod._patch_output_message([{"type": "function_call"}], "x") == ([{"type": "function_call"}], False))
_multi = [{"type": "message", "content": [{"type": "output_text", "text": "a"}]},
          {"type": "message", "content": [{"type": "output_text", "text": "b"}]}]
m2, c2 = mod._patch_output_message(_multi, "corrige")
check("plusieurs blocs message sync aussi", c2 and m2[0]["content"][0]["text"] == "corrige" and m2[1]["content"][0]["text"] == "corrige")

# ---------------------------------------------------------------- valves
check("valves max_searches défaut 6", mod.Action().Valves.model_fields["max_searches"].default == 6)
check("valves strict_search défaut True", mod.Action().Valves.model_fields["strict_search"].default is True)
check("valves correction_display défaut replace",
      mod.Action().Valves.model_fields["correction_display"].default == "replace")
_opts = mod.Action().Valves.model_fields["correction_display"].json_schema_extra["input"]["options"]
check("valves correction_display select replace/strikethrough", sorted(_opts) == ["replace", "strikethrough"])
check("valves correction_color défaut jaune",
      mod.Action().Valves.model_fields["correction_color"].default == "jaune")
_copts = mod.Action().Valves.model_fields["correction_color"].json_schema_extra["input"]["options"]
check("valves correction_color palette complète",
      sorted(_copts) == sorted(["jaune", "orange", "vert", "rose", "bleu", "cyan", "aucune"]))
check("valves show_citations défaut True", mod.Action().Valves.model_fields["show_citations"].default is True)
check("valves anciennes (dict sans correction_display) = replace",
      mod.Action().Valves.model_validate({"search_mode": "native"}).correction_display == "replace")

if FAILURES:
    print(f"\n{len(FAILURES)} ÉCHEC(S) : {FAILURES}")
    sys.exit(1)
print("\nTOUS LES TESTS PASSENT")
