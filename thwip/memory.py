"""Project memory and the second-brain vault.

One canonical `context.md` per project holds durable technical memory that every
agent used through thwip reads and helps maintain. An Obsidian vault (any folder
of Markdown files) holds one stable index card per project plus hub notes per
stack, area, and tag. Projects that share a stack, area, or tag link to each
other through those hubs and through a "Related projects" list on each card.

thwip only ever overwrites vault files it created itself (marked with
`generated_by: thwip` in their frontmatter). Existing notes are left alone.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

TEMPLATE = Path(__file__).parent / "data" / "context_template.md"
MARKER = "generated_by: thwip"
MAX_INJECT_CHARS = 8000

# File or folder that indicates a stack, in a project root. Order matters for display.
STACK_SIGNALS = [
    ("pyproject.toml", "Python"), ("requirements.txt", "Python"), ("setup.py", "Python"),
    ("package.json", "JavaScript"), ("tsconfig.json", "TypeScript"), ("Cargo.toml", "Rust"),
    ("go.mod", "Go"), ("pom.xml", "Java"), ("build.gradle", "Java"), ("Gemfile", "Ruby"),
    ("composer.json", "PHP"), ("Package.swift", "Swift"), ("pubspec.yaml", "Flutter"),
    ("Dockerfile", "Docker"), ("docker-compose.yml", "Docker"), ("platformio.ini", "Embedded"),
    ("CMakeLists.txt", "C/C++"), ("index.html", "HTML"),
]
TYPE_SIGNALS = [
    ("pyproject.toml", "cli-tool"), ("package.json", "web-app"), ("Cargo.toml", "application"),
    ("go.mod", "service"), ("platformio.ini", "firmware"), ("index.html", "website"),
]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}-", suffix=".tmp", delete=False) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _.+#-]+", "", value).strip()
    return cleaned or "untitled"


# --- frontmatter -----------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse the small YAML subset used by context files: `key: value` and `key: [a, b]`."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block, body = text[3:end].strip("\n"), text[end + 4:].lstrip("\n")
    data: dict = {}
    for line in block.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            data[key] = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
        else:
            data[key] = value.strip("'\"")
    return data, body


def render_frontmatter(data: dict) -> str:
    lines = ["---"]
    for key, value in data.items():
        if value in (None, "", []):
            continue
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(str(item) for item in value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _snapshot_field(body: str, label: str) -> str:
    """Value of a `- Label: ...` bullet in the Snapshot section, or empty."""
    match = re.search(rf"^\s*-\s*{re.escape(label)}\s*:\s*(.+)$", body, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _stack_from_body(body: str) -> list[str]:
    """Stack entries from a `- Stack: Python 3.13, rich, Vite` bullet; version suffixes are dropped."""
    raw = _snapshot_field(body, "Stack")
    items = []
    for part in re.split(r",|;|\band\b|/|\(|\)", raw):
        name = re.sub(r"\s+[\d.]+(\+)?$", "", part.strip().strip("`. "))
        name = re.sub(r"\s{2,}", " ", name)
        if not name or len(name) > 40 or not re.search(r"[A-Za-z]", name):
            continue
        if re.search(r"\b\d+\s*(MB|GB|KB)\b|\bfolder\b|\bfiles?\b", name, re.IGNORECASE):
            continue
        if name.lower() not in {"none", "not detected", "n/a", "tbd", "etc"} and name not in items:
            items.append(name)
    return items[:12]


# --- project memory ------------------------------------------------------

@dataclass
class ProjectMemory:
    project_path: str
    filename: str = "context.md"

    @property
    def path(self) -> Path:
        return Path(self.project_path).expanduser().resolve() / self.filename

    def exists(self) -> bool:
        return self.path.is_file()

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.exists() else ""

    def write(self, text: str) -> None:
        _atomic_write(self.path, text.rstrip("\n") + "\n")

    def frontmatter(self) -> dict:
        return parse_frontmatter(self.read())[0]

    def name(self) -> str:
        return str(self.frontmatter().get("project") or Path(self.project_path).resolve().name)

    def detect(self) -> dict:
        """Infer stack, type, and entry points from the repository without reading source."""
        root = Path(self.project_path).expanduser().resolve()
        present = {name for name in os.listdir(root)} if root.is_dir() else set()
        stack: list[str] = []
        for signal, label in STACK_SIGNALS:
            if signal in present and label not in stack:
                stack.append(label)
        project_type = next((label for signal, label in TYPE_SIGNALS if signal in present), "project")
        entries = [name for name in ("main.py", "app.py", "cli.py", "index.html", "src", "main.go", "Cargo.toml", "package.json") if name in present]
        return {"project": root.name, "root": str(root), "stack": stack, "type": project_type,
                "entry_points": ", ".join(entries) or "see repository", "runtime": "local"}

    def init(self, area: str = "General", purpose: str = "", tags: list[str] | None = None) -> str:
        """Create the context file from the template, filled with detected facts. Never overwrites."""
        if self.exists():
            return self.read()
        facts = self.detect()
        text = TEMPLATE.read_text(encoding="utf-8").format(
            project=facts["project"], purpose=purpose or f"{facts['project']} project", type=facts["type"],
            area=area, updated=time.strftime("%Y-%m-%d"), root=facts["root"],
            stack=", ".join(facts["stack"]), stack_text=", ".join(facts["stack"]) or "not detected",
            tags=", ".join(tags or []), entry_points=facts["entry_points"], runtime=facts["runtime"],
        )
        self.write(text)
        return text

    def touch_updated(self, text: str) -> str:
        data, body = parse_frontmatter(text)
        if not data:
            return text
        data["updated"] = time.strftime("%Y-%m-%d")
        return f"{render_frontmatter(data)}\n\n{body.lstrip()}"

    def injection(self) -> str:
        """The bounded text given to an agent as project instructions."""
        text = self.read().strip()
        if not text:
            return ""
        if len(text) > MAX_INJECT_CHARS:
            text = text[:MAX_INJECT_CHARS].rstrip() + "\n\n[Project memory truncated; read the full file with /memory]"
        return (f"Project memory ({self.filename}). This is the canonical record for this project, shared by every "
                "assistant used here. Treat it as current state; verify against the repository before relying on it; "
                f"do not rewrite it unless asked.\n\n{text}")

    def update_prompt(self, transcript: str) -> str:
        return (
            "You maintain a project's memory file. Below are the current file and the conversation that just happened.\n"
            "Return the complete updated file and nothing else, keeping the frontmatter and section order.\n"
            "Rules: change only facts that changed. Keep Current Work to at most three items: Now, Blocked, Next. "
            "Record only decisions or changes that will matter in a future session, under a dated Recent Changes heading "
            f"for {time.strftime('%Y-%m-%d')}. Do not add session logs, task lists, emojis, or filler. Keep it under 200 lines; "
            "compact older Recent Changes if needed. Update the frontmatter `updated` date.\n\n"
            f"CURRENT FILE:\n{self.read()}\n\nCONVERSATION:\n{transcript}\n"
        )


# --- vault --------------------------------------------------------------

def detect_obsidian_vaults() -> list[str]:
    """Vault folders registered with the Obsidian desktop app on this machine."""
    candidates = [
        Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json",   # macOS
        Path(os.environ.get("APPDATA", "")) / "obsidian" / "obsidian.json" if os.environ.get("APPDATA") else None,
        Path.home() / ".config" / "obsidian" / "obsidian.json",                            # Linux
        Path.home() / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian" / "obsidian.json",
    ]
    found: list[str] = []
    for candidate in candidates:
        if candidate and candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for vault in (data.get("vaults") or {}).values():
                path = vault.get("path") if isinstance(vault, dict) else None
                if isinstance(path, str) and Path(path).is_dir() and path not in found:
                    found.append(path)
    return found


@dataclass
class ProjectCard:
    name: str
    context_path: str
    area: str = ""
    stack: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    status: str = ""
    purpose: str = ""
    updated: str = ""

    @classmethod
    def from_memory(cls, memory: ProjectMemory) -> ProjectCard:
        data, body = parse_frontmatter(memory.read())
        stack = _as_list(data.get("stack")) or _stack_from_body(body)
        purpose = str(data.get("purpose", "")) or _snapshot_field(body, "Purpose")
        return cls(name=str(data.get("project") or memory.name()), context_path=str(memory.path),
                   area=str(data.get("area", "")), stack=stack, tags=_as_list(data.get("tags")),
                   status=str(data.get("status", "")), purpose=purpose, updated=str(data.get("updated", "")))

    def shares_with(self, other: ProjectCard) -> list[str]:
        shared = []
        if self.area and self.area == other.area:
            shared.append(f"area {self.area}")
        shared += [f"stack {item}" for item in self.stack if item in other.stack]
        shared += [f"tag {item}" for item in self.tags if item in other.tags]
        return shared


class Vault:
    """A folder of Markdown notes. thwip owns only the files it generated."""

    def __init__(self, root: str, cards_dir: str = "Projects"):
        self.root = Path(root).expanduser().resolve()
        self.cards_dir = cards_dir.strip("/") or "Projects"

    @property
    def cards_root(self) -> Path:
        return self.root / self.cards_dir

    @property
    def hubs_root(self) -> Path:
        """Hub notes sit at the vault root for the default layout, otherwise inside thwip's own folder."""
        return self.root if self.cards_dir == "Projects" else self.cards_root

    def _hub_link(self, kind: str, value: str) -> str:
        prefix = "" if self.cards_dir == "Projects" else f"{self.cards_dir}/"
        return f"[[{prefix}{kind}/{slug(value)}|{value}]]"

    def is_ready(self) -> bool:
        return self.root.is_dir()

    def create(self) -> None:
        self.cards_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def discover_projects(scan_roots: list[str], filename: str = "context.md") -> list[ProjectMemory]:
        """Every direct subfolder of the scan roots that holds a memory file."""
        found: list[ProjectMemory] = []
        for root in scan_roots:
            base = Path(root).expanduser()
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                if child.is_dir() and (child / filename).is_file():
                    found.append(ProjectMemory(str(child), filename))
        return found

    def sync_all(self, memories: list[ProjectMemory]) -> dict:
        """File every discovered project, then rebuild hubs and the dashboard from all of them at once."""
        report = {"written": [], "skipped": [], "projects": 0}
        for memory in memories:
            part = self.sync(memory, rebuild=False)
            report["written"] += part["written"]
            report["skipped"] += part["skipped"]
            report["projects"] += 1
        self._rebuild(self.cards(), report)
        return report

    def _managed(self, path: Path) -> bool:
        return not path.exists() or MARKER in path.read_text(encoding="utf-8")[:600]

    def cards(self) -> list[ProjectCard]:
        """Every project thwip has filed here, read back from the generated cards."""
        found: list[ProjectCard] = []
        for note in sorted(self.cards_root.glob("*.md")) if self.cards_root.is_dir() else []:
            text = note.read_text(encoding="utf-8")
            if MARKER not in text[:600]:
                continue
            data, _ = parse_frontmatter(text)
            if "project" not in data:
                continue  # the dashboard and hub notes also carry the marker
            found.append(ProjectCard(name=str(data.get("project", note.stem)), context_path=str(data.get("context_path", "")),
                                     area=str(data.get("area", "")), stack=_as_list(data.get("stack")), tags=_as_list(data.get("tags")),
                                     status=str(data.get("status", "")), purpose=str(data.get("purpose", "")), updated=str(data.get("updated", ""))))
        return found

    def sync(self, memory: ProjectMemory, rebuild: bool = True) -> dict:
        """File the project's card, refresh hub notes and the dashboard, and report what was written or skipped."""
        card = ProjectCard.from_memory(memory)
        others = [c for c in self.cards() if c.name != card.name]
        report = {"written": [], "skipped": []}
        self._write(self.cards_root / f"{slug(card.name)}.md", self._render_card(card, others), report)
        if rebuild:
            self._rebuild([*others, card], report)
        return report

    def _rebuild(self, everything: list[ProjectCard], report: dict) -> None:
        # Cards must know about each other, so re-render every card's related list from the full set.
        for card in everything:
            others = [c for c in everything if c.name != card.name]
            self._write(self.cards_root / f"{slug(card.name)}.md", self._render_card(card, others), report)
        for kind, values in (("Stack", {s for c in everything for s in c.stack}),
                             ("Areas", {c.area for c in everything if c.area}),
                             ("Tags", {t for c in everything for t in c.tags})):
            for value in sorted(values):
                members = [c for c in everything if value in (c.stack if kind == "Stack" else c.tags if kind == "Tags" else [c.area])]
                self._write(self.hubs_root / kind / f"{slug(value)}.md", self._render_hub(kind, value, members), report)
        dashboard = self.root / "Projects.md" if self.cards_dir == "Projects" else self.cards_root / "Dashboard.md"
        self._write(dashboard, self._render_dashboard(everything), report)

    def _write(self, path: Path, text: str, report: dict) -> None:
        if not self._managed(path):
            if str(path) not in report["skipped"]:
                report["skipped"].append(str(path))
            return
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return
        _atomic_write(path, text)
        if str(path) not in report["written"]:
            report["written"].append(str(path))

    def _render_card(self, card: ProjectCard, others: list[ProjectCard]) -> str:
        related = [(other, card.shares_with(other)) for other in others]
        related = [(other, shared) for other, shared in related if shared]
        lines = [render_frontmatter({
            "project": card.name, "area": card.area, "status": card.status, "updated": card.updated,
            "stack": card.stack, "tags": card.tags, "context_path": card.context_path, "generated_by": "thwip",
        }), "", f"# {card.name}", ""]
        if card.purpose:
            lines += [card.purpose, ""]
        context = Path(card.context_path) if card.context_path else None
        link = f"[context file]({context.as_uri()})" if context and context.is_absolute() else f"context file `{card.context_path or 'unknown'}`"
        lines += ["## Technical source", "",
                  (f"Current work, decisions, and history live in the project's {link}. "
                  "This card is a stable index entry maintained by thwip; do not put changing detail here."), ""]
        if card.area or card.stack or card.tags:
            lines += ["## Links", ""]
            if card.area:
                lines.append(f"- Area: {self._hub_link('Areas', card.area)}")
            for item in card.stack:
                lines.append(f"- Stack: {self._hub_link('Stack', item)}")
            for item in card.tags:
                lines.append(f"- Tag: {self._hub_link('Tags', item)}")
            lines.append("")
        lines += ["## Related projects", ""]
        if related:
            for other, shared in sorted(related, key=lambda pair: -len(pair[1])):
                lines.append(f"- [[{self.cards_dir}/{slug(other.name)}|{other.name}]]: shares {', '.join(shared)}")
        else:
            lines.append("- None yet. Projects that share a stack, area, or tag will appear here.")
        lines.append("")
        return "\n".join(lines)

    def _render_hub(self, kind: str, value: str, members: list[ProjectCard]) -> str:
        label = {"Stack": "Stack", "Areas": "Area", "Tags": "Tag"}[kind]
        lines = [render_frontmatter({"hub": label.lower(), "name": value, "generated_by": "thwip"}), "", f"# {label}: {value}", "",
                 f"Projects using this {label.lower()}, filed by thwip:", ""]
        for card in sorted(members, key=lambda c: c.name.lower()):
            detail = f" ({card.status}, updated {card.updated})" if card.status or card.updated else ""
            lines.append(f"- [[{self.cards_dir}/{slug(card.name)}|{card.name}]]{detail}")
        lines.append("")
        return "\n".join(lines)

    def _render_dashboard(self, cards: list[ProjectCard]) -> str:
        lines = [render_frontmatter({"dashboard": "projects", "generated_by": "thwip", "updated": time.strftime("%Y-%m-%d")}), "",
                 "# Projects", "", "| Project | Area | Status | Stack | Updated |", "|:--|:--|:--|:--|:--|"]
        for card in sorted(cards, key=lambda c: c.name.lower()):
            lines.append(f"| [[{self.cards_dir}/{slug(card.name)}|{card.name}]] | {card.area} | {card.status} | {', '.join(card.stack)} | {card.updated} |")
        lines.append("")
        return "\n".join(lines)
