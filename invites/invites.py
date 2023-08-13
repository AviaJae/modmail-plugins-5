from __future__ import annotations

import json

from datetime import datetime
from pathlib import Path
from typing import Optional, TypedDict, Union, TYPE_CHECKING

import discord
from discord.ext import commands
from discord.utils import MISSING

from core import checks
from core.models import getLogger, PermissionLevel
from core.paginator import EmbedPaginatorSession

from .core.models import InviteTracker, PartialInvite

# temp for migration
from .core.migration import db_migration

info_json = Path(__file__).parent.resolve() / "info.json"
with open(info_json, encoding="utf-8") as f:
    __plugin_info__ = json.loads(f.read())

__plugin_name__ = __plugin_info__["name"]
__version__ = __plugin_info__["version"]
__description__ = "\n".join(__plugin_info__["description"]).format(__version__)

# <!-- Developer -->
try:
    from discord.ext.modmail_utils import Config, ConfirmView, datetime_formatter as dt_formatter
except ImportError as exc:
    required = __plugin_info__["cogs_required"][0]
    raise RuntimeError(
        f"`modmail_utils` package is required for {__plugin_name__} plugin to function.\n"
        f"Install {required} plugin to resolve this issue."
    ) from exc


# <-- ----- -->


if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection
    from bot import ModmailBot

    class GuildConfigData(TypedDict):
        channel: str
        webhook: Optional[str]
        enable: bool


logger = getLogger(__name__)


class Invites(commands.Cog):
    __doc__ = __description__

    default_config: GuildConfigData = {
        "channel": str(int()),
        "webhook": None,
        "enable": False,
        "store_data": True,
    }

    def __init__(self, bot: ModmailBot):
        """
        Parameters
        ----------
        bot : bot.ModmailBot
            The Modmail bot.
        """
        self.bot = bot
        self.db: AsyncIOMotorCollection = bot.api.get_plugin_partition(self)
        self.config: Config = Config(self, self.db)
        self.tracker: InviteTracker = InviteTracker(self)

    async def cog_load(self) -> None:
        """
        Initial tasks when loading the cog.
        """
        self.bot.loop.create_task(self.initialize())

    async def initialize(self) -> None:
        await self.bot.wait_for_connected()
        await self.populate_config()
        await self.tracker.populate_invites()

        # temp for migration
        if not self.config.get("migrated", False):
            await db_migration(self)

    async def populate_config(self) -> None:
        """
        Populates the config cache with data from database.
        """
        self.config.defaults = {
            str(guild.id): self.config.deepcopy(self.default_config) for guild in self.bot.guilds
        }
        await self.config.fetch()

    def guild_config(self, guild_id: Union[int, str]) -> GuildConfigData:
        guild_id = str(guild_id)
        config = self.config.get(guild_id)
        if config is None:
            config = self.config.deepcopy(self.default_config)
            self.config[guild_id] = config

        return config

    async def _get_or_create_webhook(self, channel: discord.TextChannel) -> Optional[discord.Webhook]:
        """
        An internal method to retrieve an existing webhook from the channel if any, otherwise a new one
        will be created.

        Parameters
        -----------
        channel : discord.TextChannel
            The channel to get or create the webhook from.
        """
        # check bot permissions first
        bot_me = channel.guild.get_member(self.bot.user.id)
        if not bot_me or not channel.permissions_for(bot_me).manage_webhooks:
            return None

        wh = None
        webhooks = await channel.webhooks()
        if webhooks:
            # find any webhook that has token which means that belongs to the client
            wh = discord.utils.find(lambda x: x.token is not None, webhooks)

        # webhook not found, we will just create a new one
        if not wh:
            avatar = await self.bot.user.display_avatar.read()
            try:
                wh = await channel.create_webhook(
                    name=self.bot.user.name,
                    avatar=avatar,
                    reason="Webhook for invite logs.",
                )
            except Exception as e:
                logger.error(f"{type(e).__name__}: {str(e)}")
                wh = None

        return wh

    @staticmethod
    def _string_fmt_dt(dt: Optional[Union[datetime, int, float]]) -> str:
        if dt is None:
            return str(dt)
        if isinstance(dt, (int, float)):
            dt = datetime.fromtimestamp(dt)
        return discord.utils.format_dt(dt, "F")

    @commands.group(aliases=["invite"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def invites(self, ctx: commands.Context):
        """
        Set up invites tracking logs.

        **For initial setup, use commands:**
        - `{prefix}invite config channel <channel>`
        - `{prefix}invite config enable True`
        """
        await ctx.send_help(ctx.command)

    @invites.group(name="config", usage="<command> [argument]", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def invites_config(self, ctx: commands.Context):
        """
        Invites tracking configurations.

        Run this command without argument to see current set configurations.
        """
        config = self.guild_config(ctx.guild.id)

        channel = ctx.guild.get_channel(int(config["channel"]))
        embed = discord.Embed(
            title="Invites Config",
            color=self.bot.main_color,
            description="Current set configurations.",
        )

        embed.add_field(
            name="Channel:",
            value=f'{getattr(channel, "mention", "`None`")}',
            inline=False,
        )
        embed.add_field(name="Enabled:", value=f"`{config['enable']}`", inline=False)
        embed.add_field(name="Webhook URL:", value=f'`{config["webhook"]}`', inline=False)
        await ctx.send(embed=embed)

    @invites_config.command(name="channel")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def invites_config_channel(
        self, ctx: commands.Context, *, channel: Optional[discord.TextChannel] = None
    ):
        """
        Set the channel where the logs for invites tracking should be posted.

        `channel` may be a channel ID, mention, or name.

        Leave `channel` empty to see the current set channel.
        """
        config = self.guild_config(ctx.guild.id)
        if channel is None:
            channel = self.bot.get_channel(int(config.get("channel")))
            if channel:
                description = f"Invites logging channel is currently set to {channel.mention}."
            else:
                description = "Invites logging channel is not set."
        else:
            new_config = {"channel": str(channel.id), "webhook": None}
            config.update(new_config)
            await self.config.update()
            description = f"Log channel is now set to {channel.mention}."

        embed = discord.Embed(description=description, color=self.bot.main_color)
        await ctx.send(embed=embed)

    @invites_config.command(name="enable")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def invites_config_enable(self, ctx: commands.Context, *, mode: Optional[bool] = None):
        """
        Enable or disable the logging for invites tracking.

        `mode` is a boolean value, may be `True` or `False` (case insensitive).

        Examples:
        - `{prefix}invite config enable True`
        - `{prefix}invite config enable False`

        Leave `mode` empty to see the current set value.
        """
        config = self.guild_config(ctx.guild.id)
        if mode is None:
            mode = config.get("enable")
            description = (
                "Invites tracking logging is currently " + ("`enabled`" if mode else "`disabled`") + "."
            )
        else:
            new_config = {"enable": mode}
            config.update(new_config)
            description = ("Enabled " if mode else "Disabled ") + "the logging for invites tracking."
            await self.config.update()

        embed = discord.Embed(description=description, color=self.bot.main_color)
        await ctx.send(embed=embed)

        if mode:
            self.tracker.invite_cache[ctx.guild.id] = set(await ctx.guild.invites())

    @invites_config.command(name="reset")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def invites_config_reset(self, ctx: commands.Context):
        """
        Reset the configuration settings to default value.
        """
        guild_id = str(ctx.guild.id)
        self.config[guild_id] = self.config.copy(self.default_config)
        await self.config.update()

        embed = discord.Embed(
            description="Configuration settings has been reset to default.",
            color=self.bot.main_color,
        )
        embed.add_field(name="Channel:", value="`None`", inline=False)
        embed.add_field(name="Enabled:", value="`False`", inline=False)
        embed.add_field(name="Webhook URL:", value="`None`", inline=False)
        await ctx.send(embed=embed)

    @invites.group(name="store", invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def invites_store(self, ctx: commands.Context):
        """
        Base command for user data storing.

        The data storing feature was enabled by default. To disable, use command:
        - `{prefix}invite store enable False`

        If this feature is enabled, the user data and the invite used will be saved into the database when joining the server.
        When the user leaves, the data will be retrieved and the information will be added to the log embed.
        """
        await ctx.send_help(ctx.command)

    @invites_store.command(name="enable")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def inv_store_enable(self, ctx: commands.Context, *, mode: Optional[bool] = None):
        """
        Enable or disable the data storing feature.

        `mode` is a boolean value, may be `True` or `False` (case insensitive).

        Examples:
        - `{prefix}invite store enable True`
        - `{prefix}invite store enable False`

        Leave `mode` empty to see the current set value.

        __**Note:**__
        - This setting is enabled by default.
        """
        config = self.guild_config(ctx.guild.id)
        if mode is None:
            mode = config.get("store_data")
            description = (
                "Invites tracking data store is currently " + ("`enabled`" if mode else "`disabled`") + "."
            )
        else:
            new_config = {"enable": mode}
            config.update(new_config)
            description = ("Enabled " if mode else "Disabled ") + "data store for invites tracking."
            await self.config.update()

        embed = discord.Embed(description=description, color=self.bot.main_color)
        await ctx.send(embed=embed)

    @invites_store.command(name="get")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def inv_store_get(self, ctx: commands.Context, *, member: discord.Member):
        """
        Retrieve info of invite data used by a user when joining the server.

        `member` may be a user ID, mention or name.
        """
        invite = await self.tracker.get_invite_used_by(member)
        if invite is None:
            raise commands.BadArgument(f"Data for {member.name} does not exist.")

        embed = discord.Embed(
            title="User information",
            description=(f"{member.name} joined this server with invite [{invite.code}]({invite.link})."),
            color=self.bot.main_color,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"User ID: {member.id}")
        embed.add_field(name="Joined at:", value=discord.utils.format_dt(member.joined_at, "F"))
        embed.add_field(name="Invite code:", value=invite.code)

        inv_channel = invite.channel
        if inv_channel:
            inv_channel = inv_channel.mention
        else:
            channel_id = invite.channel_id
            inv_channel = f"<#{invite.channel_id}>" if channel_id else "`None`"
        embed.add_field(name="Invite channel:", value=inv_channel)

        if invite.inviter:
            inviter = f"Name: {invite.inviter.name}\nID: `{invite.inviter.id}`"
        else:
            inviter_id = invite.inviter_id
            inviter = f"(`{inviter_id}`)" if inviter_id else "`None`"
        embed.add_field(name="Invite created by:", value=inviter)
        embed.add_field(name="Invite created at:", value=self._string_fmt_dt(invite.created_at))
        await ctx.send(embed=embed)

    @invites_store.command(name="delete")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def inv_store_del(self, ctx: commands.Context, user_id: int):
        """
        Delete user data stored in the database.

        This command is global which means the user with the specified ID does not have to be in the server.
        """
        data = await self.tracker.remove_user_data(user_id)
        if data is None:
            raise commands.BadArgument(f"Data for user ID `{user_id}` does not exist.")

        embed = discord.Embed(
            description=f"Data for user ID `{user_id}` is now removed.", color=self.bot.main_color
        )
        await ctx.send(embed=embed)

    @invites_store.command(name="--clear")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def inv_store_clear(self, ctx: commands.Context):
        """
        Clear data of all users stored in the database.

        `user` may be a user ID, mention or name.

        __**Note:**__
        - This operation cannot be undone.
        """
        embed = discord.Embed(
            description=("Are you sure you want to delete all the data?"),
            color=self.bot.main_color,
        )
        view = ConfirmView(self.bot, ctx.author)
        view.message = await ctx.send(embed=embed, view=view)

        await view.wait()
        if not view.value:
            return

        try:
            await self.tracker.clear_all_data()
        except Exception as exc:
            raise commands.BadArgument(f"{type(exc).__name__}: {str(exc)}")
        embed.description = "All data cleared."
        await ctx.send(embed=embed)

    @invites.command(name="refresh")
    @checks.has_permissions(PermissionLevel.OWNER)
    async def invites_refresh(self, ctx: commands.Context):
        """
        Manually fetch the invites and store them in cache.

        **Note:**
        Invites are automatically fetched and stored in cache everytime:
         - A new member joining the server.
         - An invite being created.
        There is no need to manually fetch the invites using this command to store them in cache.
        """
        await self.tracker.populate_invites()
        embed = discord.Embed(
            description="Successfully refreshed the invite cache.",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    @invites.command(name="list")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def invites_list(self, ctx: commands.Context):
        """
        Get the list of invites on this server.
        """
        invites_list = await ctx.guild.invites()

        embeds = [
            discord.Embed(
                title="List of Invites",
                color=discord.Color.dark_theme(),
                description="",
            )
        ]
        entries = 0

        if invites_list:
            embed = embeds[0]

            for invite in reversed(sorted(invites_list, key=lambda invite: invite.uses)):
                line = f"{invite.uses} - {invite.inviter} (`{invite.inviter.id}`) - {invite.code}\n"
                if entries == 25:
                    embed = discord.Embed(
                        title="List of Invites (Continued)",
                        color=discord.Color.dark_theme(),
                        description=line,
                    )
                    embeds.append(embed)
                    entries = 1
                else:
                    embed.description += line
                    entries += 1
        else:
            embeds[0].description = "Currently there are no list of invites available."

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @invites.command(name="info")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    async def invites_info(self, ctx: commands.Context, invite: discord.Invite):
        """
        Get an info of a specific invite.
        """
        embed = discord.Embed(color=self.bot.main_color, title="__Invite Info__")
        embed.description = f"**Server:**\n{invite.guild}\n\n" f"**Invite link:**\n{invite.url}\n\n"

        inviter = f"Name: {invite.inviter.name}\nID: `{invite.inviter.id}`" if invite.inviter else "`None`"
        embed.add_field(name="Created by:", value=inviter)
        embed.add_field(name="Channel:", value=invite.channel.mention)

        fetched_invites = await ctx.guild.invites()
        if invite in fetched_invites:
            local = False
            for inv in fetched_invites:
                if invite.id == inv.id:
                    invite = inv
                    local = True
                    break
            if local:
                embed.add_field(name="Uses:", value=invite.uses)
                embed.add_field(name="Created at:", value=self._string_fmt_dt(invite.created_at))
                embed.add_field(name="Expires at:", value=self._string_fmt_dt(invite.expires_at))
        else:
            embed.description += f"**Member count:**\n{invite.approximate_member_count}\n\n"

        # could be None if the invite is from a group DM
        if invite.guild is not None:
            embed.set_thumbnail(url=str(invite.guild.icon))
            embed.set_footer(text=f"Server ID: {invite.guild.id}")

        await ctx.send(embed=embed)

    @invites.command(name="delete", aliases=["revoke"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def invites_delete(self, ctx: commands.Context, *, invite: discord.Invite):
        """
        Delete an invite.

        `invite` may be an invite code, or full invite link.
        """
        if not invite.guild or invite.guild != ctx.guild:
            raise commands.BadArgument('Invite "{}" is not from this guild.'.format(invite.code))

        embed = discord.Embed(
            color=discord.Color.blurple(),
            description=f"Deleted invite code: `{invite.code}`",
        )
        inviter = f"Name: {invite.inviter.name}\nID: `{invite.inviter.id}`" if invite.inviter else "`None`"
        embed.add_field(name="Created by:", value=inviter)
        embed.add_field(name="Channel:", value=invite.channel.mention)
        embed.add_field(name="Uses:", value=invite.uses)
        embed.add_field(name="Created at:", value=self._string_fmt_dt(invite.created_at))
        embed.add_field(name="Expires at:", value=self._string_fmt_dt(invite.expires_at))
        try:
            await invite.delete()
        except discord.Forbidden:
            raise commands.BadArgument("I do not have permissions to revoke invites.")

        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        config = self.guild_config(invite.guild.id)
        if not config["enable"]:
            return

        cached_invites = self.tracker.invite_cache.get(invite.guild.id)
        if cached_invites is None:
            cached_invites = set(await invite.guild.invites())
        else:
            cached_invites.update({invite})
        self.tracker.invite_cache[invite.guild.id] = cached_invites
        logger.debug("Invite created. Updating invite cache for guild (%s).", invite.guild)

        channel = invite.guild.get_channel(int(config["channel"]))
        if channel is None:
            return

        embed = discord.Embed(
            title="Invite created",
            color=discord.Color.blue(),
            description=invite.url,
        )
        embed.add_field(name="Created by:", value=f"Name: {invite.inviter.name}\nID: `{invite.inviter.id}`")
        embed.add_field(name="Channel:", value=str(getattr(invite.channel, "mention", None)))
        embed.add_field(name="Created at:", value=self._string_fmt_dt(invite.created_at))
        embed.add_field(name="Expires at:", value=self._string_fmt_dt(invite.expires_at))

        max_usage = str(invite.max_uses) if invite.max_uses else "Unlimited"
        embed.add_field(name="Max usage:", value=max_usage)
        await self.send_log_embed(channel, embed)

    async def send_log_embed(self, channel: discord.TextChannel, embed: discord.Embed) -> None:
        """
        Sends the log embed to the designated channel. If a webhook is available, the embed will
        be sent using the webhook instead.

        Parameters
        -----------
        channel : discord.TextChannel
            The channel to send the embed.
        embed : discord.Embed
            The embed object.
        """
        config = self.guild_config(channel.guild.id)
        wh_url = config.get("webhook")
        if wh_url is None:
            webhook = await self._get_or_create_webhook(channel)
            if webhook:
                config["webhook"] = webhook.url
                await self.config.update()
        else:
            webhook = discord.Webhook.from_url(wh_url, session=self.bot.session)

        kwargs = {"embed": embed}
        if webhook:
            kwargs["username"] = self.bot.user.name
            kwargs["avatar_url"] = self.bot.user.display_avatar.url
            kwargs["wait"] = True
            send_func = webhook.send
        else:
            send_func = channel.send
        await send_func(**kwargs)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return

        config = self.guild_config(member.guild.id)
        if not config["enable"]:
            return
        channel = member.guild.get_channel(int(config["channel"]))
        if channel is None:
            return

        embed = discord.Embed(
            title=f"{member.name} just joined.",
            color=discord.Color.green(),
            timestamp=datetime.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"User ID: {member.id}")

        join_position = sorted(member.guild.members, key=lambda m: m.joined_at).index(member) + 1
        suffix = ["th", "st", "nd", "rd", "th"][min(join_position % 10, 4)]
        if 11 <= (join_position % 100) <= 13:
            suffix = "th"

        desc = f"{member.mention} is the {join_position}{suffix} to join."
        embed.description = desc + "\n"
        embed.add_field(name="Account created:", value=dt_formatter.time_age(member.created_at))

        pred_invs = await self.tracker.get_used_invite(member)
        if pred_invs:
            vanity_inv = self.tracker.vanity_invites.get(member.guild.id)
            embed.add_field(
                name="Invite code:",
                value="\n".join(i.code if i != vanity_inv else "Vanity URL" for i in pred_invs),
            )
            embed.add_field(
                name="Invite channel:",
                value="\n".join(getattr(i.channel, "mention", "`None`") for i in pred_invs),
            )

            if len(pred_invs) == 1:
                invite = pred_invs[0]
                if invite == vanity_inv:
                    embed.add_field(name="Vanity:", value="True")
                else:
                    inviter = (
                        f"Name: {invite.inviter.name}\nID: `{invite.inviter.id}`"
                        if invite.inviter
                        else "`None`"
                    )
                    embed.add_field(name="Invite created by:", value=inviter)
                    embed.add_field(
                        name="Invite created at:",
                        value=self._string_fmt_dt(invite.created_at),
                    )
                embed.add_field(name="Invite expires:", value=self._string_fmt_dt(invite.expires_at))
                embed.add_field(name="Invite uses:", value=f"{invite.uses}")
            else:
                embed.description += "\n⚠️ *More than 1 used invites are predicted.*\n"
        else:
            embed.description += "\n⚠️ *Something went wrong! Invite info could not be resolved.*\n"
        await self.send_log_embed(channel, embed)

        if len(pred_invs) == 1 and config.get("store_data"):
            await self.tracker.save_user_data(member, pred_invs[0])

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return

        config = self.guild_config(member.guild.id)
        if not config["enable"]:
            return

        user_data = await self.tracker.get_user_data(member)
        if user_data and str(member.guild.id) in user_data["guilds"]:
            invdata = user_data["guilds"].pop(str(member.guild.id)).get("invite")
            if not user_data["guilds"] or not config.get("store_data"):
                await self.tracker.remove_user_data(member.id)
            else:
                await self.tracker.update_user_data(member, data=user_data)
        else:
            invdata = None

        channel = member.guild.get_channel(int(config["channel"]))
        if channel is None:
            return

        embed = discord.Embed(color=discord.Color.red(), timestamp=datetime.utcnow())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.title = f"{member.name} left."
        embed.set_footer(text=f"User ID: {member.id}")
        desc = f"{member.mention} just left the server."
        embed.description = desc + "\n"

        embed.add_field(name="Joined at:", value=discord.utils.format_dt(member.joined_at, "F"))
        embed.add_field(name="Time on server:", value=dt_formatter.age(member.joined_at))

        if member.nick:
            embed.description += "\n**Nickname:**\n" + member.nick + "\n"

        role_list = [role.mention for role in reversed(member.roles) if role is not member.guild.default_role]
        if role_list:
            embed.description += "\n**Roles:**\n" + (" ".join(role_list)) + "\n"

        if invdata:
            invite = await PartialInvite.from_data(self.tracker, data=invdata)
            embed.add_field(name="Invite code:", value=invdata["code"])
            inv_channel = invite.channel
            if inv_channel:
                inv_channel = inv_channel.mention
            else:
                channel_id = invite.channel_id
                inv_channel = f"<#{invite.channel_id}>" if channel_id else "`None`"
            embed.add_field(name="Invite channel:", value=inv_channel)

            if invite.inviter:
                inviter = f"Name: {invite.inviter.name}\nID: `{invite.inviter.id}`"
            else:
                inviter_id = invite.inviter_id
                inviter = f"(`{inviter_id}`)" if inviter_id else "`None`"
            embed.add_field(name="Invite created by:", value=inviter)

        await self.send_log_embed(channel, embed)


async def setup(bot: ModmailBot) -> None:
    await bot.add_cog(Invites(bot))
