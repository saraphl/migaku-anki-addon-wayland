import importlib.util
import re
import sys
import types
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def load_note_type_mgr():
    src_package = types.ModuleType("src")
    src_package.__path__ = [str(ROOT_DIR / "src")]
    sys.modules["src"] = src_package

    aqt = types.ModuleType("aqt")
    aqt.mw = None
    sys.modules["aqt"] = aqt

    anki = types.ModuleType("anki")
    anki_models = types.ModuleType("anki.models")
    anki_models.NotetypeDict = dict
    sys.modules["anki"] = anki
    sys.modules["anki.models"] = anki_models

    util = types.ModuleType("src.util")
    sys.modules["src.util"] = util

    languages = types.ModuleType("src.languages")
    languages.Language = object
    languages.Languages = object
    sys.modules["src.languages"] = languages

    spec = importlib.util.spec_from_file_location(
        "src.note_type_mgr",
        ROOT_DIR / "src" / "note_type_mgr.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


note_type_mgr = load_note_type_mgr()

published_back = """
<div>{{editable:Sentence Audio}}</div>
<div class="migaku-card-sentence">
  <div class="field" data-popup="yes" data-furigana="yes" data-pitch-coloring="yes" data-pitch-shapes="yes">{{editable:Sentence}}</div>
</div>
<div>{{editable:Word Audio}}</div>
"""

reordered_back = """
<div>{{editable:Sentence Audio}}</div>
<div>{{editable:Word Audio}}</div>
<div class="migaku-card-sentence">
  <div class="field" data-popup="yes" data-furigana="yes" data-pitch-coloring="yes" data-pitch-shapes="yes">{{editable:Sentence}}</div>
</div>
"""

skewed_back = """
<div>{{editable:Sentence Audio}}</div>
<div class="field" data-popup="yes" data-furigana="yes" data-pitch-coloring="yes" data-pitch-shapes="yes">{{editable:Word Audio}}</div>
<div class="migaku-card-sentence">{{editable:Sentence}}</div>
"""


def assert_migration(current_back):
    note_type = {"tmpls": [{"afmt": reordered_back}]}
    settings_by_name = note_type_mgr.nt_migrate_tmpl_fields_settings(
        current_back,
        reordered_back,
    )
    note_type_mgr.nt_set_tmpl_lang(
        note_type,
        None,
        0,
        "afmt",
        settings_by_name,
        commit=False,
    )

    updated_back = note_type["tmpls"][0]["afmt"]
    assert '<div>{{editable:Word Audio}}</div>' in updated_back
    assert (
        '<div class="field" data-popup="yes" data-furigana="yes" '
        'data-pitch-coloring="yes" data-pitch-shapes="yes">{{editable:Sentence}}</div>'
        in updated_back
    )


assert_migration(published_back)
assert_migration(skewed_back)

print("✓ managed note-type migration preserves and repairs field settings by name")


# The shipped front templates put the layout class on the field div itself
# ("field migaku-card-sentence"), not on a separate wrapper. Exercise the real
# templates so this path is covered by the markup the add-on actually ships.
FIELD_DIV_RE = re.compile(r'<div class="field(?P<classes>[^"]*)"')
NESTED_FIELD_RE = re.compile(r'class="field[^"]*"[^>]*>\s*<div class="field')


def assert_no_nesting(lang_dir):
    front = (lang_dir / "front.html").read_text(encoding="utf-8")

    layout_classes = {
        m.group("classes").strip()
        for m in FIELD_DIV_RE.finditer(front)
        if m.group("classes").strip()
    }
    if not layout_classes:
        return

    # A user upgrading from a release whose fields carried settings but no
    # layout classes.
    previous_front = FIELD_DIV_RE.sub('<div class="field"', front)

    note_type = {"tmpls": [{"qfmt": front}]}
    note_type_mgr.nt_set_tmpl_lang(
        note_type,
        None,
        0,
        "qfmt",
        note_type_mgr.nt_migrate_tmpl_fields_settings(previous_front, front),
        settings_mismatch_ignore=True,
        commit=False,
    )
    updated_front = note_type["tmpls"][0]["qfmt"]

    assert not NESTED_FIELD_RE.search(updated_front), (
        f"{lang_dir.parent.name}: upgrade nested a duplicate .field div, which "
        f"makes support.js process the same content twice"
    )
    for layout_class in layout_classes:
        assert layout_class in updated_front, (
            f"{lang_dir.parent.name}: upgrade dropped layout class "
            f"{layout_class!r} from the card template"
        )


language_dirs = sorted((ROOT_DIR / "src" / "languages").glob("*/card"))
assert language_dirs, "no language card directories found"
for lang_dir in language_dirs:
    assert_no_nesting(lang_dir)

print(
    "✓ shipped card templates keep their layout classes and stay unnested "
    f"on upgrade ({len(language_dirs)} languages)"
)
