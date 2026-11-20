"""Writing that reads like a knowledgeable person wrote it, built only from real facts.

Google does not penalise text for being AI-written; it penalises unhelpful, generic, mass-produced text, and
"AI detectors" are unreliable. So the aim is specific, plain, useful writing in the site's own voice:
  1. a voice profile measured from the site's existing pages,
  2. facts only from the page and the owner's answers (nothing invented),
  3. rules against filler and the phrases that make text feel machine-made,
  4. a second "editor" pass that rewrites generic lines and flags unsupported claims,
  5. automatic checks (banned phrases, sentence rhythm, em-dash and adverb overuse) on the result.
"""
from __future__ import annotations

import html
import json
import re
import statistics
from dataclasses import dataclass, field

from app.llm.client import STR, arr, obj

BANNED = [
    r"\bdelv(e|es|ing)\b", r"\belevat(e|es|ing)\b", r"\bseamless(ly)?\b", r"\bunlock(s|ing)? the\b", r"\bunleash",
    r"\bin today'?s (fast[- ]paced|digital|ever[- ]changing)", r"\bever[- ]evolving\b", r"\bgame[- ]chang(er|ing)\b",
    r"\bnavigat(e|ing) the (complex|world|landscape)", r"\b(rich|vibrant) tapestry\b", r"\btestament to\b",
    r"\bit'?s (important|worth) (to note|noting)\b", r"\bwhether you'?re\b.{0,60}\bor\b", r"\blook no further\b",
    r"\bembark\b", r"\bharness(ing)? the power\b", r"\bcutting[- ]edge\b", r"\bstate[- ]of[- ]the[- ]art\b",
    r"\bin (conclusion|summary)\b", r"\b(furthermore|moreover|additionally),", r"\brest assured\b",
    r"\bbespoke\b", r"\bworld[- ]class\b", r"\bunparalleled\b", r"\bcommitted to excellence\b",
    r"\b(robust|comprehensive|holistic) (solution|approach)s?\b", r"\bat the end of the day\b",
    r"\bnot (just|only) .{1,40}, (but|it'?s) ", r"\bdive (deep|into)\b", r"\bstreamline\b", r"\bempower(s|ing)?\b",
]
_BANNED_RX = [re.compile(p, re.I) for p in BANNED]

RULES = """How to write:
- Use only the facts given between <facts> tags. Never invent numbers, prices, dates, names, guarantees,
  awards or claims. If a fact you need is missing, leave it out rather than guess.
- Write the way an experienced person at this business would explain it to a customer: plain words,
  concrete details (model names, numbers, places, steps), no hype.
- Vary sentence length. Mix short sentences with longer ones. Don't start consecutive sentences the same way.
- Don't use filler or these phrases: delve, elevate, seamless, unlock, unleash, "in today's fast-paced world",
  ever-evolving, game-changer, "navigate the landscape", tapestry, "testament to", "it's important to note",
  "whether you're … or …", "look no further", embark, "harness the power", cutting-edge, state-of-the-art,
  "in conclusion", Furthermore/Moreover/Additionally at the start of a sentence, "rest assured", bespoke,
  world-class, unparalleled, robust/comprehensive/holistic solution, streamline, empower, "dive into",
  "not just X, but Y".
- At most one em dash per paragraph. No exclamation marks unless the site uses them. No emojis.
- No headings, bullet lists or bold text unless asked for.
- Match the voice described below (person, contractions, spelling)."""

WRITER_SYSTEM = "You write website copy for a small business, for an SEO improvement.\n" + RULES
EDITOR_SYSTEM = ("You are a strict human editor. Read the draft and rewrite any sentence that sounds generic, "
                 "salesy or machine-written, keeps the same rhythm as its neighbours, or isn't backed by the facts. "
                 "Keep every supported fact and the meaning. Return the improved text, a short list of what you "
                 "changed, and any claims in the draft that the facts don't support (they must be removed from the "
                 "text).\n" + RULES)

SCHEMAS = {
    "text": obj({"text": STR}),
    "faq": obj({"faqs": arr(obj({"question": STR, "answer": STR}))}),
}
REVIEW_SCHEMA = obj({"text": STR, "changes": arr(STR), "unsupported_claims": arr(STR)})
REVIEW_FAQ_SCHEMA = obj({"faqs": arr(obj({"question": STR, "answer": STR})), "changes": arr(STR),
                         "unsupported_claims": arr(STR)})


@dataclass
class Voice:
    person: str = "we"  # we | they
    contractions: bool = True
    spelling: str = "American"
    avg_sentence_words: float = 16.0
    exclamations: bool = False
    samples: list[str] = field(default_factory=list)

    def describe(self) -> str:
        bits = [f"Refer to the business as '{self.person}'." if self.person == "we" else
                "Refer to the business by name (third person).",
                "Use contractions (we're, you'll)." if self.contractions else "Avoid contractions.",
                f"{self.spelling} spelling.", f"Typical sentence length about {round(self.avg_sentence_words)} words.",
                "Exclamation marks are used sparingly on this site." if self.exclamations else "No exclamation marks."]
        ex = "\n".join(f"Example of the site's own writing: \"{s}\"" for s in self.samples[:2])
        return "Voice:\n" + " ".join(bits) + ("\n" + ex if ex else "")


SPELLING = [("colour", "color"), ("favour", "favor"), ("behaviour", "behavior"), ("centre", "center"),
            ("organis", "organiz"), ("optimis", "optimiz"), ("customis", "customiz"), ("recognis", "recogniz"),
            ("analys", "analyz"), ("programme", "program "), ("travell", "travel"), ("tyre", "tire")]
_SENT = re.compile(r"(?<=[.!?])\s+")


def voice_profile(texts: list[str], brand: str | None = None) -> Voice:
    text = " ".join(t for t in texts if t)[:60000]
    if len(text.split()) < 60:
        return Voice()
    words = re.findall(r"[A-Za-z']+", text.lower())
    we = sum(words.count(w) for w in ("we", "our", "us", "we're", "we've"))
    contr = len(re.findall(r"\b\w+'(re|ll|ve|s|t|d)\b", text, re.I))
    brit = sum(len(re.findall(rf"\b{b}", text, re.I)) for b, _ in SPELLING)
    amer = sum(len(re.findall(rf"\b{a}", text, re.I)) for _, a in SPELLING)
    sents = [s for s in _SENT.split(text) if 3 <= len(s.split()) <= 60]
    good = [s.strip() for s in sents if 10 <= len(s.split()) <= 30 and not any(r.search(s) for r in _BANNED_RX)]
    return Voice(person="we" if we >= 3 else "they", contractions=contr >= 3,
                 spelling="British" if brit > amer * 1.3 else "American",
                 avg_sentence_words=statistics.mean(len(s.split()) for s in sents) if sents else 16.0,
                 exclamations=text.count("!") >= 3, samples=list(dict.fromkeys(good))[:2])


def lint(text: str) -> list[str]:
    """Signs of machine-made or careless writing in the final text."""
    issues = [f"uses '{m.group(0)}'" for r in _BANNED_RX for m in [r.search(text)] if m]
    sents = [s for s in _SENT.split(text) if s.strip()]
    lens = [len(s.split()) for s in sents]
    if len(lens) >= 4 and statistics.pstdev(lens) < 3:
        issues.append("sentences are all about the same length (flat rhythm)")
    words = max(1, len(text.split()))
    if text.count("—") > max(1, words // 80):
        issues.append("too many em dashes")
    starts = [s.split()[0].lower() for s in sents if s.split()]
    if any(starts[i] == starts[i + 1] == starts[i + 2] for i in range(len(starts) - 2)):
        issues.append("three sentences in a row start the same way")
    if len(re.findall(r"\b\w+ly\b", text)) > words / 12:
        issues.append("a lot of -ly adverbs")
    return issues


@dataclass
class Written:
    text: str = ""
    faqs: list[dict] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


async def write(llm, *, kind: str, task: str, brief: str, facts: str, voice: Voice, max_words: int = 120) -> Written:
    """Draft, then edit, then check. kind: 'text' (paragraphs) or 'faq' (question/answer pairs)."""
    user = (f"{brief}\nLength: at most {max_words} words{' per answer' if kind == 'faq' else ''}.\n\n"
            f"<facts>\n{facts}\n</facts>")
    system = WRITER_SYSTEM + "\n\n" + voice.describe()
    draft = await llm.json(task=f"fix.write.{task}", role="reasoning", system=system, user=user,
                           schema=SCHEMAS[kind], max_tokens=2000)
    review_user = (f"Draft:\n<draft>\n{json.dumps(draft, ensure_ascii=False)}\n</draft>\n\n<facts>\n{facts}\n</facts>")
    edited = await llm.json(task=f"fix.edit.{task}", role="reasoning", system=EDITOR_SYSTEM + "\n\n" + voice.describe(),
                            user=review_user, schema=REVIEW_FAQ_SCHEMA if kind == "faq" else REVIEW_SCHEMA,
                            max_tokens=2000)
    out = Written(changes=edited.get("changes", []))
    if kind == "faq":
        out.faqs = [f for f in edited.get("faqs", []) if f.get("question") and f.get("answer")]
        body = " ".join(f["answer"] for f in out.faqs)
    else:
        out.text = edited.get("text", "").strip()
        body = out.text
    out.issues = lint(body) + [f"unsupported claim removed: {c}" for c in edited.get("unsupported_claims", [])]
    return out


# ------------------------------------------------------------------ rendering
def paragraphs_html(text: str) -> str:
    return "\n".join(f"<p>{html.escape(p.strip(), quote=False)}</p>" for p in re.split(r"\n\s*\n", text) if p.strip())


def faq_html(faqs: list[dict], heading: str = "Frequently asked questions") -> str:
    rows = "\n".join(f"  <h3>{html.escape(f['question'], quote=False)}</h3>\n  "
                     f"<p>{html.escape(f['answer'], quote=False)}</p>" for f in faqs)
    return f'<section class="faq">\n  <h2>{html.escape(heading, quote=False)}</h2>\n{rows}\n</section>'


def faq_jsonld(faqs: list[dict]) -> str:
    data = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": f["question"], "acceptedAnswer": {"@type": "Answer", "text": f["answer"]}}
        for f in faqs]}
    return '<script type="application/ld+json">\n' + json.dumps(data, indent=2, ensure_ascii=False) + "\n</script>"
