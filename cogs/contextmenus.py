"""Context menus — the bot answers before you even type the command.

Three doors into the same data the slash commands read:
  * user    -> Babka Profile: wallet, tickets, level
  * user    -> Collection: rarity rail and count
  * message -> Card Here: the card that message is about

The menus are registered on the command tree in cog_load(); discord.py
keeps them next to the slash commands, so one sync publishes all of it.
Names may be capitalised and spaced — unlike slash commands.
"""
import re

import discord
from discord import app_commands
from discord.ext import commands

from lang import reset_ctx_lang, set_ctx_lang, t
from utils.ui import panel

from cogs.slash import (SAMPLE_CODE, card_by_code, card_panel,
                        collection_body, wallet_body)

# Card codes look like ABC-123 / NAR-001: letters, a separator, digits.
# A date (2024-01) is deliberately not a candidate.
_CODE_RE = re.compile(r'(?<![A-Za-z0-9])([A-Za-z]{2,10}[-_][A-Za-z0-9]{1,8})(?![A-Za-z0-9])')


class ContextMenus(commands.Cog):
    """User and message context menus."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        tree = self.bot.tree
        for name, callback, kind in (
            ('Babka Profile', self._profile_menu, discord.AppCommandType.user),
            ('Collection', self._collection_menu, discord.AppCommandType.user),
            ('Card Here', self._card_here_menu, discord.AppCommandType.message),
        ):
            try:
                # A reload would otherwise trip over its own leftovers.
                tree.remove_command(name, guild=None, type=kind)
            except Exception:
                pass
            try:
                # The type is read off the callback's annotation: a member
                # means a user menu, a message means a message menu.
                menu = app_commands.ContextMenu(name=name, callback=callback)
                tree.add_command(menu)
            except Exception as e:
                print(f'[ctx] could not register "{name}": '
                      f'{type(e).__name__}: {e}', flush=True)

    # ---- helpers -----------------------------------------------------
    async def _answer(self, interaction, view=None, content=None):
        """One ephemeral answer, whatever the interaction allows."""
        try:
            if view is not None:
                await interaction.response.send_message(view=view,
                                                        ephemeral=True)
            else:
                await interaction.response.send_message(content,
                                                        ephemeral=True)
        except Exception:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(content, ephemeral=True)
                else:
                    await interaction.response.send_message(content,
                                                            ephemeral=True)
            except Exception:
                pass

    @staticmethod
    def _codes_in(message) -> list:
        """Every card-code-looking token in a message, in order."""
        found: list = []

        def scan(text):
            for hit in _CODE_RE.findall(text or ''):
                code = hit.upper()
                if code not in found:
                    found.append(code)

        for embed in getattr(message, 'embeds', None) or []:
            scan(getattr(embed, 'title', ''))
            scan(getattr(embed, 'description', ''))
            footer = getattr(embed, 'footer', None)
            if footer is not None:
                scan(getattr(footer, 'text', ''))
            for field in getattr(embed, 'fields', None) or []:
                scan(getattr(field, 'name', ''))
                scan(getattr(field, 'value', ''))
        scan(getattr(message, 'content', ''))
        for attach in getattr(message, 'attachments', None) or []:
            scan(getattr(attach, 'filename', ''))
        return found

    # ---- the menus ---------------------------------------------------
    async def _profile_menu(self, interaction: discord.Interaction,
                             member: discord.Member):
        """Babka Profile: someone's economy and level, in one panel."""
        gid = interaction.guild_id
        try:
            set_ctx_lang(interaction.user)
            if gid is None:
                return await self._answer(interaction,
                                          content=t(gid, 'slash.msg.guild_only'))
            await interaction.response.defer(ephemeral=True, thinking=True)
            body = wallet_body(gid, member.id)
            title = t(gid, 'slash.msg.ctx.profile_title',
                      user=member.display_name)
            await interaction.followup.send(
                view=panel(title, body), ephemeral=True)
        except Exception as e:
            print(f'[ctx] profile menu failed: {type(e).__name__}: {e}',
                  flush=True)
            await self._answer(interaction, content=t(gid, 'slash.msg.error'))
        finally:
            reset_ctx_lang()

    async def _collection_menu(self, interaction: discord.Interaction,
                               member: discord.Member):
        """Collection: someone's cards, counted by rarity."""
        gid = interaction.guild_id
        try:
            set_ctx_lang(interaction.user)
            if gid is None:
                return await self._answer(interaction,
                                          content=t(gid, 'slash.msg.guild_only'))
            await interaction.response.defer(ephemeral=True, thinking=True)
            body = collection_body(gid, member.id)
            title = t(gid, 'slash.msg.ctx.collection_title',
                      user=member.display_name)
            await interaction.followup.send(
                view=panel(title, body), ephemeral=True)
        except Exception as e:
            print(f'[ctx] collection menu failed: {type(e).__name__}: {e}',
                  flush=True)
            await self._answer(interaction, content=t(gid, 'slash.msg.error'))
        finally:
            reset_ctx_lang()

    async def _card_here_menu(self, interaction: discord.Interaction,
                              message: discord.Message):
        """Card Here: the card a reveal message is about, if there is one."""
        gid = interaction.guild_id
        try:
            set_ctx_lang(interaction.user)
            if gid is None:
                return await self._answer(interaction,
                                          content=t(gid, 'slash.msg.guild_only'))
            await interaction.response.defer(ephemeral=True, thinking=True)
            card = {}
            for code in self._codes_in(message):
                card = card_by_code(code)
                if card:
                    break
            if not card:
                return await interaction.followup.send(
                    t(gid, 'slash.msg.ctx.no_card') + '\n'
                    + t(gid, 'slash.msg.ctx.no_card_hint',
                        example=SAMPLE_CODE), ephemeral=True)
            await interaction.followup.send(
                view=card_panel(gid, card, interaction.user.id),
                ephemeral=True)
        except Exception as e:
            print(f'[ctx] card menu failed: {type(e).__name__}: {e}',
                  flush=True)
            try:
                await interaction.followup.send(t(gid, 'slash.msg.error'),
                                                ephemeral=True)
            except Exception:
                pass
        finally:
            reset_ctx_lang()


async def setup(bot):
    await bot.add_cog(ContextMenus(bot))
