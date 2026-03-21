import os
import sys
import json
import glob
import re
import datetime

sys.path.insert(0, os.path.dirname(__file__))
from config import VAULT_PATH
from ai_backend import get_backend, call_ai, backend_label

VAULT_PATH  = os.path.expanduser(VAULT_PATH)
OUTPUT_BASE = "Captures"

# --- GROUNDING ---
GROUNDING = (
    "IMPORTANT: Only use information explicitly present in the source text above. "
    "Do not infer, invent, or assume anything not directly written. "
    "If something is unclear, reflect that uncertainty rather than filling in the gaps."
)

# --- COLORS ---
R      = "\033[0m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
PURPLE = "\033[35m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
RED    = "\033[31m"


def parse_input(raw):
    match = re.match(r"---instructions---\s*(.*?)\s*---end---\s*", raw, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip(), raw[match.end():].strip()
    return "", raw.strip()


def collect_vault_tags():
    tags = set()
    for path in glob.glob(f"{VAULT_PATH}/**/*.md", recursive=True):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            fm = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if fm:
                for line in fm.group(1).splitlines():
                    m = re.match(r"\s*-\s*(.+)", line)
                    if m:
                        tags.add(m.group(1).strip().lower())
            for t in re.findall(r"#([a-zA-Z][a-zA-Z0-9_/-]+)", content):
                tags.add(t.lower())
        except Exception:
            continue
    return sorted(tags)


def collect_note_titles():
    titles = []
    for path in glob.glob(f"{VAULT_PATH}/**/*.md", recursive=True):
        name  = os.path.splitext(os.path.basename(path))[0]
        clean = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", name)
        titles.append(clean)
    return titles


def plan_notes(text, instructions, existing_tags, existing_titles, backend):
    tags_hint   = ", ".join(existing_tags[:60])   if existing_tags   else "none yet"
    titles_hint = ", ".join(existing_titles[:80]) if existing_titles else "none"
    instr_block = f"\nExtra instructions from the user:\n{instructions}\n" if instructions else ""

    prompt = f"""You are an expert knowledge curator for Obsidian.
{instr_block}
{GROUNDING}

Analyze the text below and decide how many notes to create.
Split only when topics are clearly distinct and each part has enough substance.
Prefer fewer, richer notes over many thin ones.

TEXT:
{text[:12000]}

EXISTING VAULT TAGS (reuse when relevant):
{tags_hint}

EXISTING VAULT NOTE TITLES (for wikilinks later):
{titles_hint}

Respond ONLY with a valid JSON array — no explanation, no markdown fences.
Each item:
{{
  "title": "meaningful note title (no date prefix)",
  "type": "conversation | meeting | project | braindump | todo | reference | journal | other",
  "tags": ["tag1", "tag2"],
  "summary": "what this note is about in 1-2 sentences",
  "related_existing": ["existing note title if truly relevant"]
}}
"""
    raw   = call_ai(prompt, backend)
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Pass 1 did not return JSON.\nRaw:\n{raw}")
    return json.loads(match.group())


def write_note_content(text, note_plan, all_titles_in_batch, instructions, existing_tags, backend):
    tags_hint     = ", ".join(existing_tags[:40]) if existing_tags else "none yet"
    batch_links   = ", ".join(f"[[{t}]]" for t in all_titles_in_batch)
    related_links = ", ".join(f"[[{t}]]" for t in note_plan.get("related_existing", []))
    instr_block   = f"\nExtra instructions from the user:\n{instructions}\n" if instructions else ""

    prompt = f"""You are writing a rich, meaningful Obsidian note.
{instr_block}
{GROUNDING}

Note title: "{note_plan['title']}"
Note type: {note_plan['type']}
What this note covers: {note_plan['summary']}

SOURCE TEXT:
{text[:12000]}

Other notes in this same batch — link with [[wikilinks]] when relevant:
{batch_links}

Existing vault notes to link when truly relevant:
{related_links if related_links else "none identified"}

Existing vault tags to reuse:
{tags_hint}

Write the full note body in Markdown. Requirements:
- Start directly with content, no title heading
- Use ## headings to organize sections naturally
- Be specific and detailed — no vague summaries, include real context
- Use [[wikilinks]] to link to other notes in this batch or existing vault notes when it genuinely adds value
- If there are tasks or follow-ups, use - [ ] checkbox format
- Write in first person if it's a personal note
- Minimum 200 words, aim for depth over brevity
- Match the language of the source text
- Do NOT invent information not present in the source text
"""
    return call_ai(prompt, backend, timeout=300)


def write_file(note_plan, body, folder_path, date_str):
    title     = note_plan.get("title", "Untitled")
    tags      = note_plan.get("tags", [])

    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
    filename   = f"{date_str} {safe_title}.md"
    filepath   = os.path.join(folder_path, filename)

    fm_lines = (
        ["---",
         "parent: ",
         "tags:"]
        + [f"  - {t}" for t in tags]
        + ["font: ",
           "creation date: " + date_str,
           "---",
           "",
           ""]
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(fm_lines) + body)

    return filepath


def main():
    args    = [a for a in sys.argv[1:] if a not in ("--api", "gemini", "ollama")]
    backend = get_backend()

    if not args:
        print(f"{RED}usage: python summarize_chat.py <file.txt>{R}")
        sys.exit(1)

    source = args[0]
    if not os.path.exists(source):
        print(f"{RED}file not found: {source}{R}")
        sys.exit(1)

    with open(source, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    instructions, text = parse_input(raw)

    if not text:
        print(f"{RED}file is empty{R}")
        sys.exit(1)

    print(f"\n{BOLD}{PURPLE}  summarize_chat{R}")
    print(f"{DIM}  source  : {source} ({len(text)} chars){R}")
    print(f"{DIM}  backend : {backend_label(backend)}{R}")
    if instructions:
        print(f"{DIM}  instructions detected{R}")
    print()

    print(f"{DIM}  scanning vault...{R}")
    existing_tags   = collect_vault_tags()
    existing_titles = collect_note_titles()
    print(f"{DIM}  {len(existing_tags)} tags · {len(existing_titles)} note titles{R}\n")

    print(f"{DIM}  pass 1: planning notes...{R}")
    try:
        plan = plan_notes(text, instructions, existing_tags, existing_titles, backend)
    except Exception as e:
        print(f"{RED}  pass 1 failed: {e}{R}")
        sys.exit(1)

    print(f"{CYAN}  {len(plan)} note(s) planned:{R}")
    for n in plan:
        print(f"    {DIM}· {n['title']} [{n['type']}]{R}")
    print()

    date_str    = datetime.datetime.now().strftime("%Y-%m-%d")
    folder_name = re.sub(r'[\\/*?:"<>|]', "", plan[0]["title"]) if plan else "dump"
    folder_path = os.path.join(VAULT_PATH, OUTPUT_BASE, f"{folder_name}")
    os.makedirs(folder_path, exist_ok=True)

    all_titles_in_batch = [n["title"] for n in plan]

    print(f"{DIM}  pass 2: writing notes...{R}\n")
    created = []
    for i, note_plan in enumerate(plan):
        title = note_plan["title"]
        print(f"  {CYAN}[{i+1}/{len(plan)}]{R} {BOLD}{title}{R}")
        try:
            body     = write_note_content(text, note_plan, all_titles_in_batch, instructions, existing_tags, backend)
            filepath = write_file(note_plan, body, folder_path, date_str)
            tags     = ", ".join(note_plan.get("tags", []))
            print(f"    {DIM}tags : {tags}{R}")
            print(f"    {DIM}path : {filepath}{R}")
            created.append(filepath)
        except Exception as e:
            print(f"    {RED}failed: {e}{R}")
        print()

    print(f"{GREEN}  done. {len(created)} note(s) saved to:{R}")
    print(f"  {BOLD}{folder_path}{R}\n")


if __name__ == "__main__":
    main()
