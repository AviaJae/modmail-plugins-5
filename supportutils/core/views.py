from __future__ import annotations

from typing import Any, Awaitable, Callable, List, Optional, Union, TYPE_CHECKING

import discord
from discord import ButtonStyle, Interaction, ui
from discord.ext import commands
from discord.ext.modmail_utils import Limit
from discord.ext.modmail_utils.ui import Button, Modal as uiModal, TextInput, View
from discord.utils import MISSING

from core.models import getLogger, DMDisabled


if TYPE_CHECKING:
    from bot import ModmailBot
    from core.thread import Thread
    from .models import ContactManager, Feedback, FeedbackManager
    from ..supportutils import SupportUtility

    ButtonCallbackT = Callable[[Union[Interaction, Any]], Awaitable]

logger = getLogger(__name__)


class Modal(uiModal):

    children: List[TextInput]

    async def on_submit(self, interaction: Interaction) -> None:
        for child in self.children:
            value = child.value
            if not value:
                # resolve empty string value
                value = None
            self.view.inputs[child.name] = value

        self.view.interaction = interaction
        await self.followup_callback(interaction, self)

    async def on_error(self, interaction: Interaction, error: Exception) -> None:
        logger.error("Ignoring exception in modal %r:", self, exc_info=error)


class DropdownMenu(ui.Select):
    def __init__(self, *, options: List[discord.SelectOption], **kwargs):
        placeholder = kwargs.pop("placeholder", "Choose option")
        self.followup_callback = kwargs.pop("callback")
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            **kwargs,
        )

    async def callback(self, interaction: Interaction) -> None:
        assert self.view is not None
        option = self.get_option(self.values[0])
        for opt in self.options:
            opt.default = opt.value in self.values
        self.view.interaction = interaction
        await self.followup_callback(interaction, self, option=option)

    def get_option(self, value: str) -> discord.SelectOption:
        for option in self.options:
            if value == option.value:
                return option
        raise ValueError(f"Cannot find select option with value of `{value}`.")


class BaseView(View):
    """
    Base view class.
    """

    children: List[Button]

    def __init__(self, cog: SupportUtility, *, message: discord.Message = MISSING, timeout: float = 300.0):
        super().__init__(message=message, timeout=timeout)
        self.cog: SupportUtility = cog
        self.bot: ModmailBot = cog.bot

    async def on_error(self, interaction: Interaction, error: Exception, item: Any) -> None:
        logger.error("Ignoring exception in view %r for item %r", self, item, exc_info=error)


class SupportUtilityView(BaseView):
    def __init__(self, ctx: commands.Context, *, input_session: str = MISSING):
        self.ctx: commands.Context = ctx
        self.user: discord.Member = ctx.author
        super().__init__(ctx.cog)
        self.input_session: str = input_session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user != self.user:
            await interaction.response.send_message(
                "These message components cannot be controlled by you.",
                ephemeral=True,
            )
            return False
        return True

    async def _action_cancel(self, *args) -> None:
        """
        Consistent callback for Cancel button.
        """
        interaction, _ = args
        self.value = None
        await interaction.response.defer()
        self.disable_and_stop()
        return


class ContactView(BaseView):
    """
    Represents a persistent view for contact panel.

    This view can only be added to the bot's message (discord limitation)
    and in the main guild.

    Parameters
    -----------
    cog : SupportUtility
        The SupportUtility cog.
    message : discord.Message
        The message object containing the view the bot listens to.
    """

    children: List[Button]

    def __init__(self, cog: SupportUtility, message: discord.Message = MISSING):
        super().__init__(cog, message=message, timeout=None)

        self.manager: ContactManager = self.cog.contact_manager
        if self.manager.view is not MISSING:
            raise RuntimeError("Another view is already attached to ContactManager instance.")
        self.manager.view = self
        self.select_options = self.manager.config["select"]["options"]

        button_config = self.manager.config["button"]
        emoji = button_config.get("emoji")
        label = button_config.get("label")
        if emoji is None and label is None:
            label = "Contact"
        try:
            style = ButtonStyle[button_config.get("style")]
        except (KeyError, TypeError, ValueError):
            style = ButtonStyle.grey
        payload = {
            "emoji": emoji,
            "label": label,
            "style": style,
            "custom_id": f"contact_button",
            "callback": self.handle_interaction,
        }
        self.add_item(Button(**payload))

    async def interaction_check(self, interaction: Interaction) -> bool:
        """
        Entry point when a user made interaction on this view's components.
        """
        user = interaction.user
        if self.bot.guild.get_member(user.id) is None:
            await interaction.response.defer()
            return False
        exists = await self.bot.threads.find(recipient=user)
        embed = discord.Embed(color=self.bot.error_color)
        if exists:
            content = "A thread for you already exists"
            if exists.channel:
                content += f" in {exists.channel.mention}"
            content += "."
            embed.description = content
        elif await self.bot.is_blocked(user):
            embed.description = f"You are currently blocked from contacting {self.bot.user.name}."
        elif self.bot.config["dm_disabled"] in (DMDisabled.NEW_THREADS, DMDisabled.ALL_THREADS):
            embed.description = self.bot.config["disabled_new_thread_response"]
            logger.info(
                "A new thread using contact menu was blocked from %s due to disabled Modmail.",
                user,
            )
        else:
            return True

        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    async def _dropdown_callback(
        self,
        interaction: discord.Interaction,
        select: DropdownMenu,
        option: discord.SelectOption,
    ) -> None:
        await interaction.response.defer()
        view = select.view
        view.inputs["contact_dropdown"] = option
        view.stop()
        await view.message.delete()

    async def handle_interaction(self, interaction: Interaction, button: Button) -> None:
        """
        Entry point for interactions on this view after all check has passed.
        Thread creation and sending response will be done from here.
        """
        await interaction.response.defer()
        user = interaction.user

        category = None
        if self.select_options:
            view = BaseView(self.cog)
            options = []
            for data in self.select_options:
                options.append(
                    discord.SelectOption(
                        emoji=data.get("emoji"), label=data["label"], description=data.get("description")
                    )
                )
            view.add_item(
                DropdownMenu(
                    options=options,
                    placeholder=self.manager.config["select"].get("placeholder"),
                    callback=self._dropdown_callback,
                )
            )
            view.message = await interaction.followup.send(view=view, ephemeral=True)
            await view.wait()
            if not view.inputs:
                return
            option = view.inputs["contact_dropdown"]
            category_id = None
            for data in self.select_options:
                if data.get("label") == option.label:
                    category_id = data.get("category")
                    break
            if category_id is None:
                raise ValueError(f"Category ID for {option.label} was not set.")
            category = self.bot.get_channel(int(category_id))
            if category is None:
                # just log, the thread will be created in main category
                logger.error(f"Category with ID {category_id} not found.")

        thread = await self.manager.create(
            recipient=user,
            category=category,
            interaction=interaction,
        )

        if thread.cancelled:
            return

        await thread.wait_until_ready()
        embed = discord.Embed(
            title="Created Thread",
            description=f"Thread started by {user.mention}.",
            color=self.bot.main_color,
        )
        await thread.channel.send(embed=embed)

    async def force_stop(self) -> None:
        """
        Stops listening to interactions made on this view and removes the view from the message.
        """
        self.stop()

        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                # just supress this
                return


class FeedbackView(BaseView):
    """
    Feedback view. This will be persistent view, which will still work after bot restart.
    However we will deal with timeout manually.
    """

    def __init__(
        self,
        user: discord.Member,
        cog: SupportUtility,
        *,
        feedback: Feedback,
        message: discord.Message = MISSING,
        thread: Optional[Thread] = None,
        timeout: Optional[float] = None,
    ):
        self.user: discord.Member = user
        self.thread: Optional[Thread] = thread
        super().__init__(cog, message=message, timeout=timeout)
        self.manager: FeedbackManager = self.cog.feedback_manager
        feedback.view = self
        self.feedback: Feedback = feedback
        self.rating: Optional[discord.SelectOption] = None

        self.add_dropdown()
        self.add_button()

    def add_dropdown(self) -> None:
        """
        Add rating dropdown if enabled. Otherwise, return silently.
        """
        rating_config = self.manager.config.get("rating", {})
        if not rating_config.get("enable", False):
            return
        options = []
        for i in reversed(range(5)):
            num = i + 1  # index zero
            options.append(discord.SelectOption(label="\N{WHITE MEDIUM STAR}" * num, value=str(num)))
        if options:
            self.add_item(
                DropdownMenu(
                    options=options,
                    placeholder=rating_config.get("placeholder"),
                    callback=self._dropdown_callback,
                    custom_id=f"feedback_dropdown",
                    row=0,
                )
            )

    def add_button(self) -> None:
        """
        Add the feedback button to this view.
        """
        button_config = self.manager.config["button"]
        emoji = button_config.get("emoji")
        label = button_config.get("label")
        if emoji is None and label is None:
            label = "Feedback"
        try:
            style = ButtonStyle[button_config.get("style")]
        except (KeyError, TypeError, ValueError):
            style = ButtonStyle.grey
        payload = {
            "emoji": emoji,
            "label": label,
            "style": style,
            "callback": self._button_callback,
            "custom_id": f"feedback_button",
        }
        self.add_item(Button(**payload))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user == self.user:
            return True
        await interaction.response.defer()
        return False

    async def _dropdown_callback(
        self,
        interaction: discord.Interaction,
        select: DropdownMenu,
        option: discord.SelectOption,
    ) -> None:
        self.rating = option
        await interaction.response.edit_message(view=select.view)

    async def _button_callback(self, *args, **kwargs) -> None:
        """
        A single callback called when user presses the feedback button attached to this view.
        """
        interaction, _ = args
        text_input = {
            "label": "Content",
            "max_length": Limit.text_input_max,
            "style": discord.TextStyle.long,
            "required": True,
        }
        modal = Modal(self, {"feedback": text_input}, self.feedback.submit, title="Feedback")
        await interaction.response.send_modal(modal)
