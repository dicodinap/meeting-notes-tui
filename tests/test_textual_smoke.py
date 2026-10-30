"""Headless Textual smoke tests using App.run_test().

These are intentionally minimal — full UI flows are slow and brittle.
We just want to catch:
  - The app actually starts without raising
  - The settings screen opens
  - Switching providers in settings doesn't crash with the duplicate-ID
    error (the bug PR #9 fixed; this is a regression guard)

These tests transitively import whisper (via meeting_notes.app →
meeting_notes.transcriber), which pulls in torch. CI deliberately skips
this file to keep install time fast — see .github/workflows/ci.yml. To
run locally:

    pip install -e ".[all,dev]"
    pytest tests/test_textual_smoke.py
"""
import pytest

# Skip the entire module if the heavy deps (whisper / textual) aren't
# installed. Avoids confusing import errors for contributors who only
# installed the lightweight test deps.
pytest.importorskip("whisper", reason="run `pip install -e .[all,dev]` to enable Textual smoke tests")
pytest.importorskip("textual", reason="run `pip install -e .[all,dev]` to enable Textual smoke tests")

from meeting_notes.app import MeetingNotesApp  # noqa: E402  (deliberate import-after-skip)


@pytest.mark.asyncio
async def test_app_starts_and_exits_cleanly(tmp_path, monkeypatch):
    """The app should mount cleanly in headless mode and respond to ctrl+c-equivalent."""
    # Sandbox config & data dirs so the test doesn't touch real ones
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    async with app.run_test() as pilot:
        # Just let the app stabilise. If anything raises during mount,
        # we'd see it here.
        await pilot.pause()
        assert app.is_running
        # Quit cleanly
        app.exit()


@pytest.mark.asyncio
async def test_settings_screen_opens(tmp_path, monkeypatch):
    """Pressing ',' should open the settings screen without error."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(",")
        await pilot.pause()
        # SettingsScreen should now be on the screen stack.
        # (We don't import it for an isinstance check — its exact import
        # path isn't load-bearing; just confirm the stack changed.)
        assert len(app.screen_stack) >= 2, "settings screen should have been pushed"
        app.exit()


@pytest.mark.asyncio
async def test_switching_providers_does_not_duplicate_widget_ids(tmp_path, monkeypatch):
    """Regression test for issue #11 / PR #9.

    Switching AI providers used to crash with `DuplicateIds: provider-openai`
    because remove_children() wasn't awaited before mount(). This test
    rapidly clicks between providers and asserts no exception.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    app = MeetingNotesApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press(",")  # open settings
        await pilot.pause()

        # Click each provider button in turn.  If remove_children isn't
        # awaited, the second mount of any provider button will raise
        # DuplicateIds.
        provider_ids = ["provider-openai", "provider-anthropic",
                        "provider-openrouter", "provider-anthropic"]
        for pid in provider_ids:
            try:
                await pilot.click(f"#{pid}")
                await pilot.pause()
            except Exception as e:
                # Surface DuplicateIds clearly if it ever comes back
                if "Duplicate" in type(e).__name__ or "already exists" in str(e):
                    pytest.fail(f"PR #9 regressed — DuplicateIds when clicking {pid}: {e}")
                # Other failures (e.g. button not found because layout
                # changed) shouldn't fail this specific regression test
                # — re-raise to fail loudly so the test gets updated.
                raise

        app.exit()


@pytest.mark.asyncio
@pytest.mark.parametrize("term_height", [40, 44, 48, 52])
async def test_system_level_meter_is_not_clipped(tmp_path, monkeypatch, term_height):
    """Regression test: the system-audio meter must actually be on screen.

    The recording view's left column is a fixed-height, non-scrolling
    Vertical. Its content used to need 31 rows, so on any terminal shorter
    than ~52 rows Textual silently clipped the last two widgets — the
    "System audio level" label and bar. A clipped meter is indistinguishable
    from a flat one, which is precisely the failure it exists to catch:
    the user sees no system-audio bar and assumes the meeting is being
    captured.
    """
    from textual.app import App, ComposeResult
    from textual.widgets import Static

    from meeting_notes.app import RecordingView

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    # Worst realistic case: the device panel is 3 lines (combined mode with
    # two long device names), which is what pushes the column over.
    device_info = (
        "🎤🔊 Microphone + System Audio\n"
        "Mic: Raptor Lake-P/U/H cAVS Digital Microphone\n"
        "System: Raptor Lake-P/U/H cAVS Speaker (monitor, auto-picked)"
    )

    class _Probe(App):
        CSS = MeetingNotesApp.CSS

        def compose(self) -> ComposeResult:
            yield RecordingView()

        def on_mount(self) -> None:
            self.query_one("#audio-device-info", Static).update(device_info)
            self.query_one("#audio-sources-list", Static).update(
                "⚠ Nothing routing to the captured sink right now"
            )

    app = _Probe()
    async with app.run_test(size=(160, term_height)) as pilot:
        await pilot.pause()
        await pilot.pause()
        visible = {
            w.id
            for w in app.screen._compositor.visible_widgets
            if getattr(w, "id", None)
        }
        for widget_id in ("level-meter-bar", "system-level-meter-label",
                          "system-level-meter-bar"):
            assert widget_id in visible, (
                f"#{widget_id} is clipped out of the recording view at a "
                f"{term_height}-row terminal"
            )
        app.exit()


@pytest.mark.asyncio
async def test_app_reports_worker_progress_and_offers_retry(tmp_path, monkeypatch):
    """The worker runs in another process, so the app has to notice its results."""
    from meeting_notes import app as app_module
    from meeting_notes.jobs import DONE, FAILED

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    started = []
    monkeypatch.setattr(app_module, "start_worker", lambda: started.append(True))
    monkeypatch.setattr(app_module, "worker_running", lambda: True)

    wav = tmp_path / "2026-10-21-204722.wav"
    wav.write_bytes(b"x")
    app = app_module.MeetingNotesApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue_recording(str(wav), "Planning", "notes")
        await pilot.pause()
        assert started == [True]
        assert app.sub_title == "Processing 1 recording(s)"
        assert app.check_action("retry_failed", ()) is False

        app.jobs.update(str(wav), status=FAILED, error="out of memory")
        app._poll_jobs()
        await pilot.pause()
        assert app.check_action("retry_failed", ()) is True
        assert "Error processing: out of memory" in [n.message for n in app._notifications]

        await pilot.press("R")
        await pilot.pause()
        assert app.jobs.all()[0].status == "pending"
        assert app.check_action("retry_failed", ()) is False

        app.jobs.update(str(wav), status=DONE, note_path="/notes/planning.md")
        app._poll_jobs()
        await pilot.pause()
        assert "✓ Note created: planning.md" in [n.message for n in app._notifications]
        assert app.sub_title == "Keyboard-driven meeting recorder"

        app.exit()


@pytest.mark.asyncio
async def test_failed_stop_does_not_queue_a_missing_recording(tmp_path, monkeypatch):
    from meeting_notes import app as app_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "start_worker", lambda: pytest.fail("worker started"))

    app = app_module.MeetingNotesApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue_recording(str(tmp_path / "missing.wav"), None, "")
        await pilot.pause()
        assert app.jobs.all() == []
        app.exit()
