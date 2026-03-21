# =============================================================
# vaultmind — study_recap.py
# =============================================================
# Run this after a study session. It auto-detects notes you
# recently modified, lets you confirm or adjust the list, then
# generates a structured recap with key concepts and review
# questions for spaced repetition.
#
# Output: Vault/Study Recaps/YYYY-MM-DD HH-MM Recap — NoteName.md
# =============================================================

import os
import sys
import glob
import json
import datetime

sys.path.insert(0, os.path.dirname(__file__))
from config import EXCLUDED_FOLDERS, VAULT_PATH, HOURS_BACK, MAX_NOTE_CHARS
from ai_backend import get_backend, call_ai, backend_label

VAULT_PATH   = os.path.expanduser(VAULT_PATH)
RECAP_FOLDER = os.path.join(VAULT_PATH, "Study Recaps")

# load prompts from prompts.json — edit that file to change the recap format
with open(os.path.join(os.path.dirname(__file__), "prompts.json"), encoding="utf-8") as f:
    PROMPTS = json.load(f)

GROUNDING = PROMPTS["grounding"]

# terminal color codes for readable output
R      = "\033[0m"   # reset
DIM    = "\033[2m"   # dimmed text
BOLD   = "\033[1m"   # bold text
CYAN   = "\033[36m"  # cyan (user prompts)
PURPLE = "\033[35m"  # purple (script header)
GREEN  = "\033[32m"  # green (success)
RED    = "\033[31m"  # red (errors)
YELLOW = "\033[33m"  # yellow (warnings/removals)


def fill_prompt(template: str, **kwargs) -> str:
    """
    Safe placeholder replacement that won't crash on literal { } in the template.
    Uses simple string replacement instead of Python's .format().
    """
    result = template
    for key, value in kwargs.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def find_recent_notes(hours: int) -> list[dict]:
    """
    Find notes modified within the last `hours` hours.
    Skips generated output folders (Briefings, Insights, Study Recaps, Captures)
    to avoid including previously generated notes in the recap.

    Args:
        hours: How many hours back to look.

    Returns:
        List of note dicts with 'name', 'path', and 'mtime' keys,
        sorted by most recently modified first.
    """
    cutoff = datetime.datetime.now() - datetime.timedelta(hours=hours)
    found  = []

    for path in sorted(
        glob.glob(f"{VAULT_PATH}/**/*.md", recursive=True),
        key=os.path.getmtime,
        reverse=True
    ):
        if any(folder in path for folder in EXCLUDED_FOLDERS):
            continue

        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        if mtime >= cutoff:
            found.append({
                "name":  os.path.basename(path),
                "path":  path,
                "mtime": mtime,
            })

    return found


def load_note(path: str) -> str:
    """
    Read a note file and return its content as a string.
    Returns an empty string if the file can't be read.

    Args:
        path: Absolute path to the .md file.

    Returns:
        File content as a string, or empty string on error.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def index_all_notes() -> dict[str, str]:
    """
    Build an index of all notes in the vault for connection finding.
    Only reads the first 500 chars of each note to keep memory usage low.
    The model uses this to identify meaningful links between studied notes
    and other notes in the vault.

    Returns:
        Dict mapping note name (without .md) to a content snippet.
    """
    notes = {}
    for path in glob.glob(f"{VAULT_PATH}/**/*.md", recursive=True):
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()[:500]
            notes[name] = content
        except Exception:
            continue
    return notes


def select_notes_interactively(auto_detected: list[dict]) -> list[dict]:
    """
    Show the auto-detected notes and let the user confirm, add, or remove them
    before generating the recap. This prevents irrelevant notes from polluting
    the output (e.g. if you opened a note briefly while studying something else).

    Commands:
        add filename.md  — add a note by filename (searches the whole vault)
        rm <number>      — remove a note by its list number
        done             — proceed with the current list

    Args:
        auto_detected: List of recently modified note dicts.

    Returns:
        Final list of selected note dicts to include in the recap.
    """
    print(f"\n{BOLD}auto-detected notes (modified in last {HOURS_BACK}h):{R}")
    selected = list(auto_detected)

    if not selected:
        print(f"  {DIM}none found{R}")
    else:
        for i, n in enumerate(selected):
            print(f"  {GREEN}{i+1}.{R} {n['name']} {DIM}({n['mtime'].strftime('%H:%M')}){R}")

    print(f"\n{DIM}commands:{R}")
    print(f"  {DIM}add filename.md  → add a note{R}")
    print(f"  {DIM}rm <number>      → remove a note{R}")
    print(f"  {DIM}done             → start generating{R}\n")

    while True:
        try:
            cmd = input(f"{CYAN}> {R}").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if cmd.lower() in ("done", ""):
            break

        elif cmd.startswith("add "):
            filename = cmd[4:].strip()
            if not filename.endswith(".md"):
                filename += ".md"
            # search the entire vault for this filename
            matches = glob.glob(f"{VAULT_PATH}/**/{filename}", recursive=True)
            if matches:
                path  = matches[0]
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
                note  = {"name": filename, "path": path, "mtime": mtime}
                if filename not in [n["name"] for n in selected]:
                    selected.append(note)
                    print(f"  {GREEN}added: {filename}{R}")
                else:
                    print(f"  {DIM}already in list{R}")
            else:
                print(f"  {RED}not found: {filename}{R}")

        elif cmd.startswith("rm "):
            try:
                idx     = int(cmd[3:].strip()) - 1
                removed = selected.pop(idx)
                print(f"  {YELLOW}removed: {removed['name']}{R}")
                # reprint the updated list
                for i, n in enumerate(selected):
                    print(f"  {GREEN}{i+1}.{R} {n['name']}")
            except (ValueError, IndexError):
                print(f"  {RED}invalid number{R}")

        else:
            print(f"  {DIM}unknown command. use add, rm or done{R}")

    return selected


def generate_recap(notes_data: list[dict], all_notes: dict, backend: str) -> str:
    """
    Send the studied notes to the AI and generate the recap.
    Uses the prompt template from prompts.json.

    The recap includes:
    - Key concepts extracted from each note
    - Review questions for spaced repetition
    - Connections to other notes in the vault

    Args:
        notes_data: List of dicts with 'name' and 'content' for each studied note.
        all_notes:  Index of all vault notes for connection finding.
        backend:    Which AI backend to use.

    Returns:
        The recap content as a Markdown string.
    """
    # format studied notes into a single block for the prompt
    notes_block = "\n\n---\n\n".join(
        f"### {n['name']}\n{n['content']}" for n in notes_data
    )
    # pass the first 100 note titles to the model for connection finding
    all_titles = ", ".join(list(all_notes.keys())[:100])

    prompt = fill_prompt(
        PROMPTS["study_recap"]["prompt"],
        grounding=GROUNDING,
        notes_block=notes_block,
        all_titles=all_titles,
    )

    return call_ai(prompt, backend, timeout=600)


def write_recap(content: str, note_names: list[str]) -> str:
    """
    Write the recap note to the Study Recaps folder in the vault.
    Creates the folder if it doesn't exist. The filename includes
    the date, time, and the first studied note's name.

    Frontmatter includes:
    - creation date
    - study-recap and review tags
    - one tag per studied note (derived from the note name)

    Args:
        content:    The recap Markdown content from generate_recap().
        note_names: List of filenames of the studied notes.

    Returns:
        The full file path of the saved note.
    """
    os.makedirs(RECAP_FOLDER, exist_ok=True)

    date_str  = datetime.datetime.now().strftime("%Y-%m-%d")
    time_str  = datetime.datetime.now().strftime("%H-%M")

    # use the first note name as the recap title
    base_name = os.path.splitext(note_names[0])[0] if note_names else "Session"
    if len(note_names) > 1:
        base_name += f" +{len(note_names)-1} more"

    filename = f"{date_str} {time_str} Recap — {base_name}.md"
    filepath = os.path.join(RECAP_FOLDER, filename)

    # generate one tag per studied note for easy filtering in Obsidian
    tags_lines = [
        "  - " + os.path.splitext(n)[0].lower().replace(" ", "-")
        for n in note_names[:5]
    ]

    fm_lines = (
        ["---", "creation date: " + date_str, "tags:", "  - study-recap", "  - review"]
        + tags_lines
        + ["---", "", ""]
    )

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(fm_lines) + content)

    return filepath


def main():
    backend = get_backend()

    print(f"\n{BOLD}{PURPLE}  study recap{R}{DIM}  spaced repetition generator{R}")
    print(f"{DIM}  backend : {backend_label(backend)}{R}")
    print(f"{DIM}  vault   : {VAULT_PATH}{R}")

    # step 1: auto-detect recently modified notes
    auto_detected = find_recent_notes(HOURS_BACK)

    # step 2: let the user confirm or adjust the list
    selected = select_notes_interactively(auto_detected)

    if not selected:
        print(f"\n{RED}  no notes selected. exiting.{R}\n")
        sys.exit(0)

    # step 3: load the full content of each selected note
    print(f"\n{DIM}  loading {len(selected)} note(s)...{R}")
    notes_data = []
    for n in selected:
        content = load_note(n["path"])
        notes_data.append({
            "name":    n["name"],
            "content": content[:MAX_NOTE_CHARS],
        })

    # step 4: index all vault notes for connection finding
    print(f"{DIM}  indexing vault for connections...{R}")
    all_notes = index_all_notes()

    # step 5: generate the recap
    print(f"{DIM}  generating recap...{R}\n")
    content  = generate_recap(notes_data, all_notes, backend)
    filepath = write_recap(content, [n["name"] for n in selected])

    print(f"\n{GREEN}✅ recap saved: {filepath}{R}\n")


if __name__ == "__main__":
    main()
