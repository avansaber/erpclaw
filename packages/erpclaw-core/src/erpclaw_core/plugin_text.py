"""Owner wording for core and model input refusals, independent of the registry and assistant."""

TEXT = {"en": {
    "files_home_missing": "ERPClaw's storage folder isn't set up yet, so the file could not be saved.",
    "files_not_these_books": ("The file could not be saved. Please ask for help restoring these books' files; "
                              "nothing was changed."),
    "render_inputs_shape": "The file could not be saved. Please try making it again; nothing was changed.",
    "file_not_bytes": "The file could not be saved. Please try making it again; nothing was changed.",
    "file_type_refused": "Only PDF files can be saved. Please choose a PDF file; nothing was changed.",
    "file_size_refused": "A stored file holds at least one byte and at most 10 MB.",
    "file_document": ("The file could not be saved. Please choose the document it belongs to; "
                      "nothing was changed."),
    "file_not_saved": "The file could not be saved safely.",
    "file_not_found": "No such stored file.",
    "file_altered": "The stored file has been altered or lost.",
    "model_input_invalid": "The assistant asked for something it can't do, so nothing was saved.",
    "plugin_boundary_failed": "Something went wrong inside your books' software, so nothing was changed.",
    "plugin_boundary_currency_unknown": ("These books do not have the following currencies, so nothing was "
                                         "changed: {currencies}."),
}}
LANGUAGE = "en"


def say(key: str, **kw) -> str:
    return TEXT[LANGUAGE][key].format(**kw)
