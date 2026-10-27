"""robots.txt read the way Google and Yandex read it: `*` is any run of characters, `$` anchors the end, the longest
matching rule wins and Allow wins a tie. urllib.robotparser knows neither `*` nor `$` — a `Disallow: /*?page=` of
workspace.ru would simply be ignored — so the readers of sites with such rules use this (services/ws_tenders.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass


def _pattern(rule: str) -> re.Pattern[str]:
    anchored = rule.endswith("$")
    body = re.escape(rule[:-1] if anchored else rule).replace(r"\*", ".*")
    return re.compile("^" + body + ("$" if anchored else ""))


@dataclass(frozen=True)
class RobotsRules:
    rules: tuple[tuple[bool, str, re.Pattern[str]], ...] = ()

    @classmethod
    def parse(cls, text: str, agent: str = "") -> RobotsRules:
        """The group of `agent` (a product token, e.g. «JobSearchPanel») if the file has one, else the group of `*`.
        Consecutive User-agent lines share the rules that follow them."""
        groups: dict[str, list[tuple[bool, str]]] = {}
        current: list[str] = []
        in_rules = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if in_rules:
                    current, in_rules = [], False
                current.append(value.lower())
                for name in current:
                    groups.setdefault(name, [])
            elif key in ("allow", "disallow"):
                in_rules = True
                if value:  # an empty Disallow allows everything: no rule
                    for name in current:
                        groups[name].append((key == "allow", value))
        token = agent.lower().split("/", 1)[0]
        chosen = groups.get(token) if token and token in groups else groups.get("*", [])
        return cls(tuple((allow, rule, _pattern(rule)) for allow, rule in chosen or []))

    def allowed(self, path: str) -> bool:
        """`path` with its query string, e.g. «/tenders/crm/?page=2»."""
        best: tuple[int, bool] | None = None
        for allow, rule, rx in self.rules:
            if rx.match(path):
                key = (len(rule), allow)
                if best is None or key > best:
                    best = key
        return True if best is None else best[1]
