"""The Ukrainian catalog ``locale/uk/LC_MESSAGES/messages.po`` holds each message of ``messages.pot``, translated.

The tests read the ``.po`` file, so the CI needs no compiled ``.mo`` file. The catalog problems come from one function,
and the tests of the function change a copy of the real catalog, so each check shows that it fails when it must.
"""

import copy
import re
from pathlib import Path

import pytest
from babel.messages import Catalog
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

from youtube_automanager import i18n
from youtube_automanager.i18n import _, set_language

I18N_DIR = Path(i18n.__file__).resolve().parent
TEMPLATE = I18N_DIR / "messages.pot"
CATALOG = I18N_DIR / "locale" / "uk" / "LC_MESSAGES" / "messages.po"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _read(path: Path, locale: str | None = None) -> Catalog:
    with path.open("rb") as file:
        return read_po(file, locale=locale)


def _placeholders(text: object) -> set[str]:
    """Return the names of the ``{placeholders}`` of a message text. The template has no plural form."""
    assert isinstance(text, str), f"A plural form is not expected: {text!r}"
    return set(PLACEHOLDER.findall(text))


def catalog_problems(template: Catalog, catalog: Catalog) -> list[str]:
    """Return a line for each way that ``catalog`` fails to translate ``template`` in full."""
    problems = []
    if catalog.fuzzy:
        problems.append("the header of the catalog is fuzzy")
    wanted = {message.id for message in template if message.id}
    for msgid in sorted(wanted):
        message = catalog.get(msgid)
        if message is None:
            problems.append(f"missing: {msgid!r}")
            continue
        if message.fuzzy:
            problems.append(f"fuzzy: {msgid!r}")
        if not message.string or not message.string.strip():
            problems.append(f"untranslated: {msgid!r}")
        elif _placeholders(msgid) != _placeholders(message.string):
            problems.append(f"placeholders differ: {msgid!r} -> {message.string!r}")
    problems.extend(f"obsolete: {message.id!r}" for message in catalog.obsolete.values())
    problems.extend(
        f"not in the template: {message.id!r}" for message in catalog if message.id and message.id not in wanted
    )
    return problems


@pytest.fixture(scope="module")
def template() -> Catalog:
    return _read(TEMPLATE)


@pytest.fixture(scope="module")
def uk() -> Catalog:
    return _read(CATALOG, locale="uk")


def _changed(catalog: Catalog) -> Catalog:
    """Return a copy of the catalog that a test can change."""
    return copy.deepcopy(catalog)


def test_the_ukrainian_catalog_translates_each_message_of_the_template(template, uk):
    assert len(template) > 400, "The template has too few messages"

    problems = catalog_problems(template, uk)

    assert not problems, "\n".join(problems[:20]) + "\nIn the repository root, run: just pot, just po"


def test_a_message_without_a_translation_is_a_problem(template, uk):
    changed = _changed(uk)
    changed["Dashboard"].string = ""

    assert catalog_problems(template, changed) == ["untranslated: 'Dashboard'"]


def test_a_translation_of_blanks_is_a_problem(template, uk):
    changed = _changed(uk)
    changed["Dashboard"].string = "  "

    assert catalog_problems(template, changed) == ["untranslated: 'Dashboard'"]


def test_a_fuzzy_message_is_a_problem(template, uk):
    changed = _changed(uk)
    changed["Dashboard"].flags.add("fuzzy")

    assert catalog_problems(template, changed) == ["fuzzy: 'Dashboard'"]


def test_a_fuzzy_header_is_a_problem(template, uk):
    changed = _changed(uk)
    changed.fuzzy = True

    assert catalog_problems(template, changed) == ["the header of the catalog is fuzzy"]


def test_a_lost_placeholder_is_a_problem(template, uk):
    changed = _changed(uk)
    changed["Run {id} started."].string = "Запуск почався."

    assert catalog_problems(template, changed) == [
        "placeholders differ: 'Run {id} started.' -> 'Запуск почався.'",
    ]


def test_a_new_placeholder_is_a_problem(template, uk):
    changed = _changed(uk)
    changed["Dashboard"].string = "Огляд {id}"

    assert catalog_problems(template, changed) == ["placeholders differ: 'Dashboard' -> 'Огляд {id}'"]


def test_a_message_that_the_catalog_lacks_is_a_problem(template, uk):
    changed = _changed(uk)
    del changed["Dashboard"]

    assert catalog_problems(template, changed) == ["missing: 'Dashboard'"]


def test_a_message_that_the_template_lacks_is_a_problem(template, uk):
    changed = _changed(uk)
    changed.add("A text of an old page", "Текст старої сторінки")

    assert catalog_problems(template, changed) == ["not in the template: 'A text of an old page'"]


def test_an_obsolete_message_is_a_problem(template, uk):
    changed = _changed(uk)
    changed.obsolete["Old text"] = copy.copy(changed["Dashboard"])
    changed.obsolete["Old text"].id = "Old text"

    assert catalog_problems(template, changed) == ["obsolete: 'Old text'"]


def test_the_ukrainian_catalog_compiles_and_the_service_shows_it(uk, tmp_path, monkeypatch):
    compiled = tmp_path / "locale" / "uk" / "LC_MESSAGES" / f"{i18n.DOMAIN}.mo"
    compiled.parent.mkdir(parents=True)
    with compiled.open("wb") as file:
        write_mo(file, uk)
    monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path / "locale")

    assert set_language("uk") == "uk"

    assert _("Dashboard") == "Огляд"
    assert _("Settings") == "Налаштування"
    assert _("Run {id} started.").format(id=7) == "Запуск 7 почався."
    # A message with quotes in the English text finds its translation.
    assert _('Delete the rule "{name}"?').format(name="Музика") == "Видалити правило «Музика»?"
