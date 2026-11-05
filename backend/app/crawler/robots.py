"""robots.txt: matching (via Protego) plus a group parser for reporting."""
from __future__ import annotations

from dataclasses import dataclass, field

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
        """Most specific group for an agent (exact token match), else the '*' group."""
        a = agent.lower()
        star = None
        for g in self.groups:
            names = [x.lower() for x in g.agents]
            if a in names:
                return g
            if "*" in names and star is None:
                star = g
        return star

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
