"""The editor agent: the untrusted party whose edits everything else checks."""

from ais.editor.scripted import ScriptedEditor, EditorError

__all__ = ["ScriptedEditor", "EditorError"]
