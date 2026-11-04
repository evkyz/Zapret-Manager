import re
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Strategy:
    name: str
    stem: str
    argv: list[str]


def _tokenize_args(blob: str) -> list[str]:
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(blob)

    while i < n:
        ch = blob[i]

        if quote:
            if ch == quote:
                quote = None
            else:
                buf.append(ch)
        else:
            if ch in ('"', "'"):
                quote = ch
            elif ch in (' ', '\t', '\r', '\n'):
                if buf:
                    tokens.append("".join(buf))
                    buf = []
            else:
                buf.append(ch)
        i += 1

    if buf:
        tokens.append("".join(buf))

    return tokens


def parse_bat(bat_path: Path) -> Strategy | None:
    base_dir = bat_path.parent
    bin_dir = base_dir / "bin"
    lists_dir = base_dir / "lists"

    text = bat_path.read_text(encoding="utf-8", errors="ignore")

    joined = re.sub(r"\^\s*\r?\n\s*", " ", text)

    m = re.search(r'"[^"]*winws\.exe"\s*(.*?)(?:\r?\n|$)', joined, re.DOTALL)
    if not m:
        return None

    args_blob = m.group(1)

    args_blob = args_blob.replace("%BIN%", str(bin_dir) + "\\")
    args_blob = args_blob.replace("%LISTS%", str(lists_dir) + "\\")

    args_blob = args_blob.replace("%GameFilterTCP%", "")
    args_blob = args_blob.replace("%GameFilterUDP%", "")

    tokens = _tokenize_args(args_blob)

    cleaned: list[str] = []
    for t in tokens:
        if not t:
            continue
        if t in ("^", "^^"):
            continue

        t = t.replace("^!", "!")

        if "=" in t and t.endswith(","):
            t = t.rstrip(",")
            if t.endswith("="):
                continue

        if "=" in t:
            key, _, value = t.partition("=")
            if value.strip() == "":
                continue

        cleaned.append(t)

    if not cleaned:
        return None

    return Strategy(name=bat_path.name, stem=bat_path.stem, argv=cleaned)


def find_strategies(zapret_dir: Path, debug: bool = False) -> list[Strategy]:
    result: list[Strategy] = []
    for bat in sorted(zapret_dir.glob("general*.bat")):
        if bat.name.lower().startswith("service"):
            continue
        s = parse_bat(bat)
        if s:
            if debug:
                print(f"\n=== {bat.name} ===")
                print(f"  всего токенов: {len(s.argv)}")
                for i, a in enumerate(s.argv):
                    print(f"  [{i}] {a!r}")
            result.append(s)
    return result