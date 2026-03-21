# =============================================================
# vaultmind — txt_to_notes.py
# =============================================================
# Converts any text file into one or more structured Obsidian
# notes. Works with conversations, meeting notes, articles,
# braindumps — anything you can paste into a .txt file.
#
# Two-pass process:
#   Pass 1: AI reads the text and decides how many notes to create,
#           planning titles, types, and tags.
#   Pass 2: AI writes each note with full content, reusing existing
#           vault tags and creating wikilinks between notes.
#
# Optional instructions block at the top of your .txt file:
#   ---instructions---
#   Write in first person. Focus on what I built.
#   ---end---
#
# Output: Vault/Captures/YYYY-MM-DD title/
# =============================================================

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
OUTPUT_BASE = "Captures"  # all output goes under Vault/Captures/

# load prompts from prompts.json — edit that file to change note format
with open(os.path.join(os.path.dirname(__file__), "prompts.json"), encoding="utf-8") as f:
    PROMPTS = json.load(f)

GROUNDING = PROMPTS["grounding"]

# terminal color codes
R      = "\033[0m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
PURPLE = "\033[35m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
RED    = "\033[31m"


def fill_prompt(template: str, **kwargs) -> str:
    """
    Safe placeholder replacement that won't crash on literal { } in the template.
    Uses simple string replacement instead of Python's .format() to avoid
    KeyErrors when the prompt contains JSON examples with curly braces.

    Args:
        template: The prompt template string with {placeholder} markers.
        **kwargs: Key-value pairs to substitute into the template.

    Returns:
        The prompt with all placeholders replaced.
    """
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def parse_input(raw: str) -> tuple[str, str]:
    """
    Parse the input file for an optional instructions block.
    If found, separates instructions from content.

    Instructions block format:
        ---instructions---
        your instructions here
        ---end---
        (your content below)

    Args:
        raw: Full contents of the input .txt file.

    Returns:
        Tuple of (instructions, content). Instructions is empty string if not found.
    """
    match = re.match(r"---instructions---\s*(.*?)\s*---end---\s*", raw, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip(), raw[match.end():].strip()
    return "", raw.strip()


def collect_vault_tags() -> list[str]:
    """
    Scan all notes in the vault and collect existing tags.
    Reads both YAML frontmatter tags and inline #hashtags.
    These are passed to the AI so it reuses existing tags
    instead of inventing new inconsistent ones.

    Returns:
        Sorted list of unique tag strings.
    """
    tags = set()

    for path in glob.glob(f"{VAULT_PATH}/**/*.md", recursive=True):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            # extract tags from YAML frontmatter
            fm = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if fm:
                for line in fm.group(1).splitlines():
                    m = re.match(r"\s*-\s*(.+)", line)
                    if m:
                        tags.add(m.group(1).strip().lower())

            # extract inline #hashtags from note body
            for t in re.findall(r"#([a-zA-Z][a-zA-Z0-9_/-]+)", content):
                tags.add(t.lower())

        except Exception:
            continue

    return sorted(tags)


def collect_note_titles() -> list[str]:
    """
    Collect all note titles from the vault for wikilink generation.
    Strips date prefixes (e.g. '2026-03-20 ') from filenames so the
    model can reference notes by their clean title.

    Returns:
        List of note title strings without .md extension or date prefix.
    """
    titles = []
    for path in glob.glob(f"{VAULT_PATH}/**/*.md", recursive=True):
        name  = os.path.splitext(os.path.basename(path))[0]
        clean = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", name)
        titles.append(clean)
    return titles


def plan_notes(
    text: str,
    instructions: str,
    existing_tags: list[str],
    existing_titles: list[str],
    backend: str
) -> list[dict]:
    """
    Pass 1: Ask the AI to analyze the text and plan how many notes to create.
    Returns a list of note plans (title, type, tags, summary, related notes).
    The actual note content is written in pass 2.

    Args:
        text:            The source text to convert.
        instructions:    Optional user instructions from the file header.
        existing_tags:   Tags already used in the vault.
        existing_titles: Note titles already in the vault.
        backend:         Which AI backend to use.

    Returns:
        List of note plan dicts parsed from the model's JSON response.

    Raises:
        ValueError: If the model doesn't return valid JSON.
    """
    tags_hint   = ", ".join(existing_tags[:60])   if existing_tags   else "none yet"
    titles_hint = ", ".join(existing_titles[:80]) if existing_titles else "none"
    instr_block = f"\nExtra instructions from the user:\n{instructions}\n" if instructions else ""

    prompt = fill_prompt(
        PROMPTS["txt_to_notes"]["plan_prompt"],
        instructions_block=instr_block,
        grounding=GROUNDING,
        text=text[:12000],
        tags_hint=tags_hint,
        titles_hint=titles_hint,
    )

    raw = call_ai(prompt, backend)

    # strip markdown code fences if the model wrapped the JSON in ```
    raw = re.sub(r"```(?:json)?", "", raw).strip()

    # find the JSON array in the response
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError(f"Model did not return a JSON array.\nRaw output:\n{raw[:500]}")

    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from model: {e}\nMatched:\n{match.group()[:500]}")


def write_note_content(
    text: str,
    note_plan: dict,
    all_titles_in_batch: list[str],
    instructions: str,
    existing_tags: list[str],
    backend: str
) -> str:
    """
    Pass 2: Write the full content for a single note.
    The model knows about all other notes being created in this batch
    so it can create meaningful [[wikilinks]] between them.

    Args:
        text:                The full source text (same for all notes).
        note_plan:           The plan dict for this specific note from pass 1.
        all_titles_in_batch: Titles of all notes being created in this run.
        instructions:        Optional user instructions from the file header.
        existing_tags:       Tags already used in the vault.
        backend:             Which AI backend to use.

    Returns:
        The note body as a Markdown string.
    """
    tags_hint     = ", ".join(existing_tags[:40]) if existing_tags else "none yet"
    batch_links   = ", ".join(f"[[{t}]]" for t in all_titles_in_batch)
    related_links = ", ".join(f"[[{t}]]" for t in note_plan.get("related_existing", [])) or "none identified"
    instr_block   = f"\nExtra instructions from the user:\n{instructions}\n" if instructions else ""

    prompt = fill_prompt(
        PROMPTS["txt_to_notes"]["write_prompt"],
        instructions_block=instr_block,
        grounding=GROUNDING,
        title=note_plan["title"],
        type=note_plan["type"],
        summary=note_plan["summary"],
        text=text[:12000],
        batch_links=batch_links,
        related_links=related_links,
        tags_hint=tags_hint,
    )

    return call_ai(prompt, backend, timeout=300)


def write_file(note_plan: dict, body: str, folder_path: str, date_str: str) -> str:
    """
    Write a single note to disk with proper Obsidian frontmatter.
    Sanitizes the title for use as a filename.

    Frontmatter includes:
    - parent (empty, for Obsidian hierarchy plugins)
    - tags from the note plan
    - font (empty, for Obsidian font plugins)
    - creation date

    Args:
        note_plan:   The note plan dict with title and tags.
        body:        The note body Markdown from write_note_content().
        folder_path: The output folder path.
        date_str:    Today's date string (YYYY-MM-DD).

    Returns:
        The full file path of the saved note.
    """
    title      = note_plan.get("title", "Untitled")
    tags       = note_plan.get("tags", [])

    # remove characters that are invalid in filenames
    safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
    filename   = f"{date_str} {safe_title}.md"
    filepath   = os.path.join(folder_path, filename)

    fm_lines = (
        ["---", "parent: ", "tags:"]
        + [f"  - {t}" for t in tags]
        + ["font: ", "creation date: " + date_str, "---", "", ""]
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(fm_lines) + body)

    return filepath


def main():
    args    = [a for a in sys.argv[1:] if a != "--api"]
    backend = get_backend()

    if not args:
        print(f"{RED}usage: python txt_to_notes.py <file.txt>{R}")
        sys.exit(1)

    source = args[0]
    if not os.path.exists(source):
        print(f"{RED}file not found: {source}{R}")
        sys.exit(1)

    with open(source, "r", encoding="utf-8", errors="ignore") as f:
        raw = f.read()

    # separate instructions block from content
    instructions, text = parse_input(raw)

    if not text:
        print(f"{RED}file is empty{R}")
        sys.exit(1)

    print(f"\n{BOLD}{PURPLE}  txt_to_notes{R}")
    print(f"{DIM}  source  : {source} ({len(text)} chars){R}")
    print(f"{DIM}  backend : {backend_label(backend)}{R}")
    if instructions:
        print(f"{DIM}  instructions detected{R}")
    print()

    # collect existing vault tags and titles for the AI to reuse
    print(f"{DIM}  scanning vault...{R}")
    existing_tags   = collect_vault_tags()
    existing_titles = collect_note_titles()
    print(f"{DIM}  {len(existing_tags)} tags · {len(existing_titles)} note titles{R}\n")

    # pass 1: plan the notes
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

    # create a dated subfolder for all notes from this run
    date_str    = datetime.datetime.now().strftime("%Y-%m-%d")
    folder_name = re.sub(r'[\\/*?:"<>|]', "", plan[0]["title"]) if plan else "dump"
    folder_path = os.path.join(VAULT_PATH, OUTPUT_BASE, f"{date_str} {folder_name}")
    os.makedirs(folder_path, exist_ok=True)

    # collect all planned titles so pass 2 can cross-link between them
    all_titles_in_batch = [n["title"] for n in plan]

    # pass 2: write each note
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
