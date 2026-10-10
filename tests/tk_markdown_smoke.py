"""Exercise source/preview editing on a real Tk display, optionally save a screenshot."""
import argparse
from pathlib import Path
import sys
import tkinter as tk
from tkinter import font

from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import CommentEditor

SAMPLE = '''# Bauteilbeschreibung

Ein **kräftiger Sensor** mit *präziser Messung* und ***kombinierter Betonung***.

- Anschluss: `24 V DC`
- Zustand: **geprüft**
  - Gehäuse ohne Schäden

1. Versorgung verbinden
2. Messwert ablesen

> Vor Montage die Versorgung abschalten.

[Datenblatt](https://example.org/datenblatt)

```text
Pin 1  +24 V
Pin 2  Signal
Pin 3  GND
```

~~Alte Kennzeichnung~~ → neue Kennzeichnung
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshot', type=Path)
    args = parser.parse_args()
    root = tk.Tk()
    root.title('ChaosBox — Markdown comment preview')
    root.geometry('720x760+10+10')
    errors = []
    root.report_callback_exception = lambda kind, error, traceback: errors.append(error)
    editor = CommentEditor(root)
    editor.pack(fill='both', expand=True, padx=16, pady=16)
    editor.source.insert('1.0', SAMPLE)
    editor.select(1)
    root.update()
    assert not errors, errors
    assert editor.source.get('1.0', 'end-1c') == SAMPLE
    rendered = editor.preview.get('1.0', 'end-1c')
    assert '# Bauteil' not in rendered and '**kräftiger' not in rendered
    assert '• Anschluss: 24 V DC' in rendered and '1. Versorgung verbinden' in rendered
    assert 'Pin 1  +24 V' in rendered
    assert editor.preview.cget('state') == 'disabled'
    tags = [tag for tag in editor.preview.tag_names() if tag.startswith('md_span_')]
    assert any(font.Font(font=editor.preview.tag_cget(tag, 'font')).actual('weight') == 'bold'
               and font.Font(font=editor.preview.tag_cget(tag, 'font')).actual('slant') == 'italic' for tag in tags)
    if args.screenshot:
        ImageGrab.grab(bbox=(root.winfo_rootx(), root.winfo_rooty(),
                            root.winfo_rootx() + root.winfo_width(), root.winfo_rooty() + root.winfo_height()),
                       xdisplay=root.winfo_screen()).save(args.screenshot)
    # Switching modes and rendering must not modify source or its undo history.
    editor.select(0)
    editor.source.edit_reset()
    editor.source.insert('end', '\n**Additional note**')
    editor.source.edit_separator()
    editor.select(1)
    root.update()
    assert 'Additional note' in editor.preview.get('1.0', 'end-1c')
    editor.source.edit_undo()
    assert editor.source.get('1.0', 'end-1c') == SAMPLE
    editor.refresh()
    assert 'Additional note' not in editor.preview.get('1.0', 'end-1c')
    # Repeated renders must not accumulate Tcl link callbacks.
    command_count = len(editor.preview._tclCommands or [])
    for _ in range(5):
        editor.refresh()
    assert len(editor.preview._tclCommands or []) == command_count
    editor.source.delete('1.0', 'end')
    editor.refresh()
    assert editor.preview.get('1.0', 'end-1c') == ''
    root.update()
    root.destroy()
    assert not errors, errors
    print('PASS: Markdown preview, combined fonts, source/undo preservation, refresh and cleanup')


if __name__ == '__main__':
    main()
