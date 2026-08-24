"""Config flow for the Everrise Dashboard Config Bridge integration."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_FILENAME,
    CONF_FOLDER,
    DEFAULT_FILENAME,
    DEFAULT_FOLDER,
    DOMAIN,
    FILENAME_PATTERN,
    FOLDER_PATTERN,
)


def _validate(folder: str, filename: str) -> dict[str, str]:
    """Return a dict of field->error for any invalid input, empty if all OK."""
    errors: dict[str, str] = {}
    if not re.fullmatch(FOLDER_PATTERN, folder):
        errors[CONF_FOLDER] = "invalid_folder"
    if not re.fullmatch(FILENAME_PATTERN, filename):
        errors[CONF_FILENAME] = "invalid_filename"
    return errors


def _schema(folder: str = DEFAULT_FOLDER, filename: str = DEFAULT_FILENAME) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_FOLDER, default=folder): str,
            vol.Optional(CONF_FILENAME, default=filename): str,
        }
    )


class EverriseDashboardConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup — one instance per Home Assistant install.

    Most clients never need to touch the defaults; the folder/filename
    fields exist for the rare case where a dashboard was deployed under a
    different www/ subfolder name than "everrise-dashboard".
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}
        if user_input is not None:
            folder = user_input[CONF_FOLDER].strip()
            filename = user_input[CONF_FILENAME].strip()
            errors = _validate(folder, filename)
            if not errors:
                return self.async_create_entry(
                    title=f"Everrise Dashboard ({folder})",
                    data={},
                    options={CONF_FOLDER: folder, CONF_FILENAME: filename},
                )
            # Re-show the form with whatever the user typed, so a typo isn't
            # discarded back to the defaults.
            return self.async_show_form(
                step_id="user", data_schema=_schema(folder, filename), errors=errors
            )

        return self.async_show_form(step_id="user", data_schema=_schema())

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EverriseDashboardOptionsFlow:
        return EverriseDashboardOptionsFlow(config_entry)


class EverriseDashboardOptionsFlow(config_entries.OptionsFlow):
    """Lets the installer fix a typo'd folder/filename later without
    deleting and re-adding the integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        current_folder = self._entry.options.get(CONF_FOLDER, DEFAULT_FOLDER)
        current_filename = self._entry.options.get(CONF_FILENAME, DEFAULT_FILENAME)

        errors: dict[str, str] = {}
        if user_input is not None:
            folder = user_input[CONF_FOLDER].strip()
            filename = user_input[CONF_FILENAME].strip()
            errors = _validate(folder, filename)
            if not errors:
                return self.async_create_entry(
                    title="", data={CONF_FOLDER: folder, CONF_FILENAME: filename}
                )
            return self.async_show_form(
                step_id="init", data_schema=_schema(folder, filename), errors=errors
            )

        return self.async_show_form(
            step_id="init", data_schema=_schema(current_folder, current_filename)
        )
