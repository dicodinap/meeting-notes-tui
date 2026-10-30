#!/bin/bash
# Pipeline: note/transcript -> Claude Code CLI -> Obsidian vault
#
# Optional. Requires the `claude` CLI, and sends the transcript to Claude.
# Configure it in ~/.config/meeting-notes/process-meeting.conf
# (see process-meeting.conf.example).
set -euo pipefail

CONF_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/meeting-notes/process-meeting.conf"
if [ ! -f "$CONF_FILE" ]; then
    echo "Error: $CONF_FILE not found. Copy process-meeting.conf.example there and edit it."
    exit 1
fi

VAULT_ROOT=""
NOTES_DIR="$HOME/Documents/meeting-notes/notes"
ROUTES=()
# shellcheck source=/dev/null
source "$CONF_FILE"

if [ -z "$VAULT_ROOT" ] || [ "${#ROUTES[@]}" -eq 0 ]; then
    echo "Error: set VAULT_ROOT and at least one entry in ROUTES in $CONF_FILE"
    exit 1
fi

if ! command -v claude &> /dev/null; then
    echo "Error: the claude CLI is not installed."
    exit 1
fi

# Argument or latest note
TRANSCRIPT_FILE="${1:-}"
if [ -z "$TRANSCRIPT_FILE" ]; then
    TRANSCRIPT_FILE=$(ls -t "$NOTES_DIR"/*.md 2>/dev/null | head -1 || true)
fi

if [ -z "$TRANSCRIPT_FILE" ] || [ ! -f "$TRANSCRIPT_FILE" ]; then
    echo "Error: no transcript found. Usage: ./process-meeting.sh [file.md]"
    exit 1
fi

echo "→ Processing: $(basename "$TRANSCRIPT_FILE")"
echo "→ Calling Claude..."

ROUTE_LINES=""
FOLDER_NAMES=""
for route in "${ROUTES[@]}"; do
    IFS='|' read -r route_kind route_folder route_description <<< "$route"
    ROUTE_LINES+="- \"$route_folder\" ($route_kind): $route_description"$'\n'
    FOLDER_NAMES+="${FOLDER_NAMES:+ | }$route_folder"
done

PROMPT="You are processing a transcript for an Obsidian second-brain vault.
The transcript is either a work MEETING or a university CLASS (lecture).

## ROUTING
Pick exactly ONE folder based on content:

$ROUTE_LINES
How to tell a class from a meeting: a class has one dominant speaker explaining
theory or concepts, mentions of exams, assignments, units or the semester,
students asking questions, or academic content being taught. A meeting has
several participants coordinating work, deadlines, clients or deliverables.
If nothing matches clearly, pick the closest folder.

## LANGUAGE
Write the note, including section headings, in the language of the transcript.

## OUTPUT FORMAT
Respond with EXACTLY these 3 header lines, then the note. No preamble, no explanation, no code fences:

FOLDER: <$FOLDER_NAMES>
TITLE: <YYYY-MM-DD - short description>
---
<note content starts here>

## NOTE CONTENT
Start directly with the YAML frontmatter block (no code fences around it):

---
date: <YYYY-MM-DD>
tags: [<meeting OR class>, <topic-tags>]
participants: [<names if mentioned>]
folder: <the folder you chose>
---

Then, IF the folder is a meeting folder, use these sections:

## Summary
- 3-5 bullet points on the main topics

## Key decisions
- decisions made. If none: \"None recorded.\"

## Next steps
- [ ] action (owner if mentioned)

## Full transcript
<verbatim transcript>

Then, IF the folder is a class folder, use these sections INSTEAD:

## Summary
- 3-5 bullet points on the topics covered

## Key concepts
- term or concept: definition or explanation given in class. One per bullet.

## Assessments and dates
- tests, deliveries or deadlines mentioned, with their date. If none: \"None mentioned.\"

## Pending tasks
- [ ] task, reading or exercise assigned. If none: \"None assigned.\"

## Full transcript
<verbatim transcript>

---
TRANSCRIPT TO PROCESS:
"

# Piped through stdin: a single argv entry is capped at 128 KiB on Linux, which
# a long meeting can exceed.
RESPONSE=$( { printf '%s\n' "$PROMPT"; cat "$TRANSCRIPT_FILE"; } | claude -p )

# Parse header lines
FOLDER=$(printf '%s\n' "$RESPONSE" | sed -n '/^FOLDER:/{s/^FOLDER:[[:space:]]*//;s/[[:space:]]*$//;p;q}')
TITLE=$(printf '%s\n' "$RESPONSE" | sed -n '/^TITLE:/{s/^TITLE:[[:space:]]*//;s/[[:space:]]*$//;p;q}')
TITLE=${TITLE//\//-}

# Validate folder; an unknown answer falls back to the first route
KIND=""
for route in "${ROUTES[@]}"; do
    IFS='|' read -r route_kind route_folder route_description <<< "$route"
    if [ "$route_folder" = "$FOLDER" ]; then
        KIND="$route_kind"
        break
    fi
done
if [ -z "$KIND" ]; then
    IFS='|' read -r KIND route_folder route_description <<< "${ROUTES[0]}"
    echo "⚠ Unexpected folder '$FOLDER' — defaulting to $route_folder"
    FOLDER="$route_folder"
fi

if [ "$KIND" = "class" ]; then
    BASE_DIR="$VAULT_ROOT/classes"
else
    BASE_DIR="$VAULT_ROOT/meetings"
fi

# Fallback title if Claude didn't produce one
if [ -z "$TITLE" ]; then
    TITLE="$(date +%Y-%m-%d) - $KIND"
fi

# Content starts after the first --- separator
CONTENT=$(printf '%s\n' "$RESPONSE" | sed '1,/^---$/d')

mkdir -p "$BASE_DIR/$FOLDER"
OUTPUT_FILE="$BASE_DIR/$FOLDER/${TITLE}.md"

printf '%s\n' "$CONTENT" > "$OUTPUT_FILE"

echo ""
echo "✓ Type   : $KIND"
echo "✓ Folder : $FOLDER"
echo "✓ Title  : $TITLE"
echo "✓ Saved  : $OUTPUT_FILE"
