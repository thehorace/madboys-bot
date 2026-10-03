"""Track completed UI callbacks without counting attempts as successful usage."""
import logging

import discord

log = logging.getLogger("madboys-bot.interactions")


def record(interaction, outcome):
    try:
        from cogs.usage import log_interaction
        log_interaction(interaction, outcome)
    except Exception:
        log.exception("Couldn't record interaction outcome")


def failed(interaction):
    interaction.extras["usage_status"] = "failed"


class TrackedView(discord.ui.View):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        for child in self.children:
            self._track(child)

    def _track(self, item):
        callback = item.callback
        if getattr(callback, "_usage_tracked", False) is True or getattr(item, "url", None):
            return

        async def tracked(interaction):
            record(interaction, "pending")
            try:
                await callback(interaction)
            except Exception:
                failed(interaction)
                record(interaction, "failed")
                raise
            else:
                record(interaction, interaction.extras.get("usage_status", "success"))

        tracked._usage_tracked = True
        item.callback = tracked

    def add_item(self, item):
        self._track(item)
        return super().add_item(item)

    async def on_error(self, interaction, error, item):
        failed(interaction)
        record(interaction, "failed")
        log.error("UI callback failed", exc_info=error)
        try:
            text = "Something went wrong. Please try again."
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass
