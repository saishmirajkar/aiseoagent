"""robots.txt: matching (via Protego) plus a group parser for reporting."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from protego import Protego

AI_BOTS = ["GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "CCBot"]


@dataclass
class RobotsGroup:
    agents: list[str]
    rules: list[tuple[str, str]] = field(default_factory=list)  # (allow|disallow, path)
    crawl_delay: float | None = None


@dataclass
class RobotsInfo:
    url: str
    status: int | None
    text: str = ""
    error: str | None = None
    groups: list[RobotsGroup] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    _parser: Protego | None = None

    @property
    def fetched_ok(self) -> bool:
        return self.status == 200

    @property
    def unreachable(self) -> bool:
        """5xx / network failure: Google treats the whole site as disallowed."""
        return self.status is None or self.status >= 500

    def can_fetch(self, url: str, agent: str) -> bool:
        if self.status is not None and 400 <= self.status < 500:
            return True  # 4xx robots.txt = no restrictions
        if self.unreachable:
            return False
        if self._parser is None:
            self._parser = Protego.parse(self.text)
        return self._parser.can_fetch(url, agent)

    def crawl_delay(self, agent: str) -> float | None:
        if not self.fetched_ok:
            return None
        if self._parser is None:
            self._parser = Protego.parse(self.text)
        d = self._parser.crawl_delay(agent)
        return float(d) if d is not None else None

    def group_for(self, agent: str) -> RobotsGroup | None:
        """Rules that apply to an agent: all groups naming it (merged, as Google does), else all '*' groups."""
        a = agent.lower()
        exact = [g for g in self.groups if a in [x.lower() for x in g.agents]]
        chosen = exact or [g for g in self.groups if "*" in [x.lower() for x in g.agents]]
        if not chosen:
            return None
        if len(chosen) == 1:
            return chosen[0]
        merged = RobotsGroup(agents=list(dict.fromkeys(x for g in chosen for x in g.agents)))
        for g in chosen:
            merged.rules.extend(g.rules)
            merged.crawl_delay = merged.crawl_delay or g.crawl_delay
        return merged

    def matching_rule(self, url: str, agent: str) -> str | None:
        """The rule that decides access for ``url`` (longest match wins; Allow wins ties), e.g. 'Disallow: /contact'."""
        g = self.group_for(agent)
        if g is None:
            return None
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best: tuple[int, int, str] | None = None
        for kind, rule in g.rules:
            if not rule:
                continue
            rx = "^" + re.escape(rule).replace(r"\*", ".*").replace(r"\$", "$")  # robots wildcards
            if re.match(rx, path):
                key = (len(rule), 1 if kind == "allow" else 0, f"{kind.capitalize()}: {rule}")
                if best is None or key[:2] > best[:2]:
                    best = key
        return best[2] if best else None

    def ai_bot_rules(self, origin: str, bots: list[str] | None = None) -> dict[str, dict]:
        out = {}
        for bot in bots or AI_BOTS:
            g = self.group_for(bot)
            specific = g is not None and bot.lower() in [x.lower() for x in g.agents]
            out[bot] = {
                "root_allowed": self.can_fetch(origin + "/", bot),
                "has_specific_group": specific,
                "disallow": [p for d, p in (g.rules if g else []) if d == "disallow" and p][:20],
            }
        return out


def parse_groups(text: str) -> tuple[list[RobotsGroup], list[str]]:
    groups: list[RobotsGroup] = []
    sitemaps: list[str] = []
    current: RobotsGroup | None = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "sitemap":
            if value:
                sitemaps.append(value)
            continue
        if key == "user-agent":
            if current is None or not last_was_agent:
                current = RobotsGroup(agents=[])
                groups.append(current)
            current.agents.append(value)
            last_was_agent = True
            continue
        last_was_agent = False
        if current is None:
            continue
        if key in ("allow", "disallow"):
            current.rules.append((key, value))
        elif key == "crawl-delay":
            try:
                current.crawl_delay = float(value)
            except ValueError:
                pass
    return groups, sitemaps


def build_robots(url: str, status: int | None, text: str, error: str | None = None) -> RobotsInfo:
    info = RobotsInfo(url=url, status=status, text=text if status == 200 else "", error=error)
    if status == 200:
        info.groups, info.sitemaps = parse_groups(text)
    return info
