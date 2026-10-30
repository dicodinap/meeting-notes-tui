MEETING NOTES AI - SETUP GUIDE FOR CODING AGENTS

You are a coding agent. The user cloned this repository and wants it working
on their machine. Follow these steps in order, from the repository root.
Humans can follow them too. Features and usage are documented in README.md.

What it does: a Linux terminal app that records microphone and system audio,
transcribes locally with Whisper, and optionally summarizes with an LLM.


RULES

- Linux only, with PipeWire or PulseAudio.
- Get the user's approval before running sudo or installing system packages.
- Never ask the user to paste an API key or Notion token into the chat. Never
  print one, and never write one to a file inside this repository.
- Do not run setup.sh, setup_local.sh or setup_cloud.sh. They wait for
  keyboard input, and the steps below replace them.
- If ~/.config/meeting-notes/config.yaml already exists, show the user what
  you would change and get approval before overwriting it.
- Recordings and transcripts stay on this machine. They are gitignored, and
  must stay that way.


STEP 1 - ASK THE USER

Ask these together, then continue without further questions.

1. Summaries:
   - none: transcripts only. Free and private. Use this if they have no
     preference.
   - local: Ollama. Free and private, but slower.
   - anthropic, openai or openrouter: cloud. Needs an API key, and the
     transcript is sent to that provider.
2. Hardware: is there an NVIDIA GPU they want Whisper to use? If unsure, use
   the CPU. It is the default and works everywhere.
3. Whisper model, if they care: tiny, base (default), small, medium or large.
   Larger is more accurate but slower.
4. Note pipeline (step 6): send structured notes to Obsidian, Notion, both or
   neither. It needs the Claude Code CLI and sends transcripts to Claude. Notion
   also receives the full transcript.


STEP 2 - SYSTEM PACKAGES

Check what is missing:

    for t in pactl parec pw-record ffmpeg python3; do
        command -v "$t" || echo "MISSING: $t"
    done

Python must be 3.10 or newer. Install only what is missing:

    Arch:          sudo pacman -S --needed libpulse pipewire-audio ffmpeg
    Debian/Ubuntu: sudo apt install pulseaudio-utils pipewire-bin ffmpeg python3-venv
    Fedora:        sudo dnf install pulseaudio-utils pipewire-utils ffmpeg-free

Confirm the audio server responds:

    pactl info | grep 'Server Name'


STEP 3 - PYTHON ENVIRONMENT

The venv must be named venv, because meeting-notes.sh expects that path.

CPU only. Installing the CPU build of torch first saves about 3 GB of CUDA
libraries:

    python3 -m venv venv
    venv/bin/pip install --upgrade pip
    venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
    venv/bin/pip install -r requirements.txt

NVIDIA GPU: skip the torch line. Later, use WHISPER_DEVICE = "cuda" in step 4.

Verify:

    venv/bin/python -c "import whisper, textual, yaml; print('ok')"
    venv/bin/pip install pytest pytest-asyncio
    venv/bin/python -m pytest tests -q

All tests should pass. If any fail, tell the user which ones, and fix the
cause before continuing.


STEP 4 - CONFIGURATION

Without this file the app defaults to Anthropic and shows a missing-key
warning. Edit the first four values to match step 1, then run:

    venv/bin/python - <<'PY'
    from pathlib import Path
    from meeting_notes.config import AppConfig, save_config, validate_config

    AI_PROVIDER = "none"   # none, local, anthropic, openai, openrouter
    AI_MODEL = "none"      # see the table below
    WHISPER_MODEL = "base"
    WHISPER_DEVICE = "cpu" # cpu, cuda, auto

    notes = Path.home() / "Documents/meeting-notes/notes"
    notes.mkdir(parents=True, exist_ok=True)
    config = AppConfig(
        ai_provider=AI_PROVIDER,
        ai_model=AI_MODEL,
        whisper_model=WHISPER_MODEL,
        whisper_device=WHISPER_DEVICE,
        notes_dir=str(notes),
    )
    save_config(config)
    print(validate_config(config))
    PY

    Provider     AI_MODEL
    none         none
    local        an Ollama tag, such as llama3.2:3b
    anthropic    haiku or sonnet
    openai       mini or standard
    openrouter   cheap, balanced or premium

A cloud provider reports a missing key at this point until the next section
is done. The config file is created with mode 600.

API key (cloud only). Ask the user to do one of these themselves, in their
own terminal:
- Open the app, press comma for Settings, and enter the key there.
- Export it from their shell startup file: ANTHROPIC_API_KEY, OPENAI_API_KEY
  or OPENROUTER_API_KEY.

Ollama (local only). Show the user this command before running it, because it
downloads and runs an install script:

    curl -fsSL https://ollama.com/install.sh | sh

Then pull the model, replacing the tag with their choice:

    ollama pull llama3.2:3b


STEP 5 - COMMAND AND SHORTCUTS

Install the launcher, and check that ~/.local/bin is on PATH:

    mkdir -p ~/.local/bin
    ln -sf "$PWD/meeting-notes.sh" ~/.local/bin/meeting-notes

Hyprland or Waybar users: offer the snippets in hyprland/ and the Waybar
section of README.md. Do not edit their desktop configuration unless they
agree.


STEP 6 - OBSIDIAN AND NOTION PIPELINE (OPTIONAL)

Skip this unless the user asked for it in step 1. After each recording, the
background worker has Claude turn the transcript into a structured note and
send it to the destinations they chose. It runs whenever the config file below
exists.

Check that the claude command exists. If it is missing, see
https://code.claude.com/docs/en/overview

    conf_dir="${XDG_CONFIG_HOME:-$HOME/.config}/meeting-notes"
    mkdir -p "$conf_dir"
    cp process-meeting.conf.example "$conf_dir/process-meeting.conf"

Edit that copy:
- DESTINATIONS: ("obsidian"), ("notion") or ("obsidian" "notion").
- VAULT_ROOT: the absolute path to their vault. Only needed for Obsidian.
- NOTES_DIR: must match notes_dir from step 4.
- ROUTES: ask how they organize their vault, and write one entry per folder.
  Each folder description is what Claude uses to choose the destination, so
  be specific.

Personal details belong in this config file, never in process-meeting.sh.

Notion only. Ask the user to do these themselves:
1. Create an internal connection in the Notion Developer portal
   (https://developers.notion.com). Creating one requires being a Workspace
   Owner. The connection needs the insert content capability.
2. Open the Notion page that should hold the notes, open the ... menu, choose
   Add connections, and select the connection. Skipping this causes a 404.
3. In their own editor, set NOTION_PARENT_PAGE and NOTION_TOKEN in the config
   file, or export NOTION_TOKEN from their shell startup file.

Then restrict the file:

    chmod 600 "$conf_dir/process-meeting.conf"

Every note becomes a new page under that one parent page.


STEP 7 - FINISH

The app needs a real terminal and audio, so hand this part to the user:

1. Run: meeting-notes
2. Press A for the Audio Test. It checks the microphone and system audio.
3. Press r to record, play some audio, then press s to stop.

The first transcription downloads the Whisper model (140 MB for base).
Whisper on CPU is slow: expect about 30 minutes for an 85-minute meeting.
Processing runs in the background, so they can quit the app while it works.

Report to the user what you installed, what you configured, and anything you
skipped. Remind them to tell people when a meeting is being recorded, since
consent rules vary by location.


TROUBLESHOOTING

- No system audio in the recording: run the Audio Test, then set the output
  device under Settings. README.md has an Audio Configuration section.
- "no kernel image is available" or other CUDA errors: set whisper_device to
  cpu in ~/.config/meeting-notes/config.yaml.
- A recording failed: press R in the app to retry. Finished steps are not
  repeated. The queue is ~/.config/meeting-notes/jobs.json, and errors are in
  errors.log in the same folder.
- Recordings from before the queue existed: run
  venv/bin/python run.py --add <file.wav>
- Configuration lives in ~/.config/meeting-notes/config.yaml.
- Relative paths such as recordings and transcripts resolve from the directory
  the app starts in. meeting-notes.sh always starts it in the repository root.
