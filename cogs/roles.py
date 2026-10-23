"""
Roles Cog for Logiq
Self-assignable roles with modal-based setup
"""

import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List
import logging
import time
import uuid

from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_admin
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)


MAX_MENU_ROLES = 25


def role_entry(role: discord.Role) -> dict:
    """Dati di un ruolo per il menu (solo emoji unicode: l'icona del ruolo non è un emoji valido)"""
    return {'role': role, 'emoji': role.unicode_emoji or "🎭", 'label': role.name[:100]}


def build_menu_view(role_list: List[dict], exclusive: bool, title: str, menu_id: Optional[str] = None):
    if exclusive:
        return ExclusiveRoleView(role_list, title, menu_id)
    return MultiRoleView(role_list, menu_id)


class RoleMenuSetupModal(discord.ui.Modal, title="Create Role Menu"):
    """Modal for creating role menus with custom settings"""

    title_input = discord.ui.TextInput(
        label="Menu Title",
        placeholder="e.g., Choose Your Roles",
        required=True,
        max_length=100
    )

    description_input = discord.ui.TextInput(
        label="Menu Description",
        placeholder="e.g., Select your preferred roles from the dropdown below",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    role_mentions = discord.ui.TextInput(
        label="Roles (mention with @)",
        placeholder="Type @ and select roles. Example: @Gamer @Artist @Developer",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )

    exclusive = discord.ui.TextInput(
        label="Exclusive? (yes/no)",
        placeholder="Type 'yes' if users can only pick ONE role",
        required=True,
        max_length=3
    )

    def __init__(self, cog, channel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        import re
        
        # Parse exclusive setting
        is_exclusive = self.exclusive.value.lower() in ['yes', 'y', 'true']

        # Parse role mentions
        role_list = []
        text = self.role_mentions.value
        role_ids = re.findall(r'<@&(\d+)>', text)

        if not role_ids:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Roles Found", "Please mention roles using @. Type @ and select roles from the list that appears."),
                ephemeral=True
            )
            return

        for role_id in role_ids:
            role = interaction.guild.get_role(int(role_id))
            if role:
                # Skip @everyone and bot integration roles
                if role.is_default() or role.is_integration():
                    continue
                    
                role_emoji = None
                if role.unicode_emoji:
                    role_emoji = role.unicode_emoji

                role_list.append({
                    'role': role,
                    'emoji': role_emoji or "🎭",
                    'label': role.name
                })

        if not role_list:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Valid Roles", f"Found {len(role_ids)} role mentions but they cannot be used (might be bot roles or @everyone)."),
                ephemeral=True
            )
            return

        if len(role_list) > 25:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Too Many Roles", "Discord allows maximum 25 options per menu."),
                ephemeral=True
            )
            return

        await self.cog.post_menu(
            self.channel, self.title_input.value, self.description_input.value,
            role_list, is_exclusive, interaction.user.id
        )

        # Respond to interaction
        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Role Menu Created!",
                f"{'Exclusive' if is_exclusive else 'Multi-select'} role menu created in {self.channel.mention}"
            ),
            ephemeral=True
        )

        logger.info(f"Role menu created by {interaction.user} with {len(role_list)} roles")


class ExclusiveRoleSelect(discord.ui.Select):
    """Dropdown for exclusive role selection (pick only one)"""

    def __init__(self, role_data: List[dict], category_name: str, menu_id: Optional[str] = None):
        options = [
            discord.SelectOption(
                label=r['label'],
                description=f"Get the {r['label']} role",
                value=str(r['role'].id),
                emoji=r['emoji']
            )
            for r in role_data[:25]
        ]

        super().__init__(
            placeholder=f"Choose your option...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"rm:{menu_id}" if menu_id else f"exclusive_role_{category_name[:50]}"
        )
        self.role_ids = [r['role'].id for r in role_data]

    async def callback(self, interaction: discord.Interaction):
        """Handle exclusive role selection - LOCKED after first selection"""
        try:
            # Check if user already has any role from THIS MENU ONLY
            user_has_role = False
            existing_role = None
            for role_id in self.role_ids:
                role = interaction.guild.get_role(role_id)
                if role and role in interaction.user.roles:
                    user_has_role = True
                    existing_role = role
                    break

            if user_has_role:
                await interaction.response.send_message(
                    embed=EmbedFactory.error(
                        "🔒 Role Already Selected",
                        f"You already have **{existing_role.name}**. You cannot select another role from this menu."
                    ),
                    ephemeral=True
                )
                return

            selected_role_id = int(self.values[0])
            selected_role = interaction.guild.get_role(selected_role_id)

            if not selected_role:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Error", "Role not found"),
                    ephemeral=True
                )
                return

            # Give the selected role (only this one, no removing others)
            await interaction.user.add_roles(selected_role, reason="Exclusive role menu selection")

            embed = EmbedFactory.success(
                "✅ Role Selected!",
                f"You now have the **{selected_role.name}** role!\n\n"
                f"**Note:** You cannot select another role from this menu."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

            logger.info(f"{interaction.user} selected exclusive role {selected_role.name}")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to manage your roles. Please contact an admin."),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in role selection: {e}", exc_info=True)
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", f"Failed to assign role: {str(e)}"),
                ephemeral=True
            )


class MultiRoleSelect(discord.ui.Select):
    """Dropdown menu for multiple role selection"""

    def __init__(self, role_data: List[dict], menu_id: Optional[str] = None):
        options = [
            discord.SelectOption(
                label=r['label'],
                description=f"Toggle {r['label']} role",
                value=str(r['role'].id),
                emoji=r['emoji']
            )
            for r in role_data[:25]
        ]

        super().__init__(
            placeholder="Select roles to add/remove...",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"rm:{menu_id}" if menu_id else "multi_role_select"
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle role selection"""
        try:
            selected_role_ids = {int(value) for value in self.values}
            current_role_ids = {role.id for role in interaction.user.roles}

            roles_to_add = []
            roles_to_remove = []

            available_role_ids = {int(option.value) for option in self.options}

            for role_id in available_role_ids:
                role = interaction.guild.get_role(role_id)
                if not role:
                    continue

                if role_id in selected_role_ids and role_id not in current_role_ids:
                    roles_to_add.append(role)
                elif role_id not in selected_role_ids and role_id in current_role_ids:
                    roles_to_remove.append(role)

            if roles_to_add:
                await interaction.user.add_roles(*roles_to_add, reason="Role menu selection")
            if roles_to_remove:
                await interaction.user.remove_roles(*roles_to_remove, reason="Role menu deselection")

            changes = []
            if roles_to_add:
                changes.append(f"**Added:** {', '.join([r.name for r in roles_to_add])}")
            if roles_to_remove:
                changes.append(f"**Removed:** {', '.join([r.name for r in roles_to_remove])}")

            if not changes:
                changes.append("No changes made")

            embed = EmbedFactory.success(
                "✅ Roles Updated!",
                "\n".join(changes)
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to manage your roles. Please contact an admin."),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in multi-role selection: {e}", exc_info=True)
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", f"Failed to update roles: {str(e)}"),
                ephemeral=True
            )


class ExclusiveRoleView(discord.ui.View):
    """View for exclusive role selection"""

    def __init__(self, role_data: List[dict], category_name: str, menu_id: Optional[str] = None):
        super().__init__(timeout=None)
        self.add_item(ExclusiveRoleSelect(role_data, category_name, menu_id))


class MultiRoleView(discord.ui.View):
    """View for multi role selection"""

    def __init__(self, role_data: List[dict], menu_id: Optional[str] = None):
        super().__init__(timeout=None)
        self.add_item(MultiRoleSelect(role_data, menu_id))


class Roles(commands.Cog):
    """Role management cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('roles', {})
        # Register persistent views on startup
        self.bot.loop.create_task(self._register_persistent_views())
    
    async def _register_persistent_views(self):
        """Dopo un riavvio i menu già pubblicati tornano a funzionare"""
        await self.bot.wait_until_ready()
        try:
            menus = await self.db.get_all_role_menus()
        except Exception as e:
            logger.error(f"Impossibile ricaricare i menu dei ruoli: {e}")
            return
        restored = 0
        for doc in menus:
            guild = self.bot.get_guild(doc.get('guild_id'))
            if not guild:
                continue
            roles = [guild.get_role(rid) for rid in doc.get('role_ids', [])]
            role_list = [role_entry(r) for r in roles if r][:MAX_MENU_ROLES]
            if not role_list:
                continue
            view = build_menu_view(role_list, bool(doc.get('exclusive')), doc.get('title', ''), doc['menu_id'])
            self.bot.add_view(view, message_id=doc['message_id'])
            restored += 1
        logger.info(f"Role menu persistent views restored: {restored}")

    async def post_menu(self, channel, title: str, description: Optional[str], role_list: List[dict],
                        exclusive: bool, created_by: int) -> dict:
        """Pubblica un menu di ruoli nel canale e lo salva, così sopravvive ai riavvii"""
        menu_id = uuid.uuid4().hex[:12]
        embed = EmbedFactory.create(
            title=title,
            description=description or "Select your roles from the dropdown below.",
            color=EmbedColor.PRIMARY
        )
        embed.add_field(
            name="Available Roles",
            value="\n".join(f"{r['emoji']} {r['role'].mention}" for r in role_list),
            inline=False
        )
        message = await channel.send(embed=embed, view=build_menu_view(role_list, exclusive, title, menu_id))
        doc = {
            'menu_id': menu_id,
            'guild_id': channel.guild.id,
            'channel_id': channel.id,
            'message_id': message.id,
            'title': title,
            'description': description or '',
            'exclusive': bool(exclusive),
            'role_ids': [r['role'].id for r in role_list],
            'created_by': created_by,
            'created_at': time.time(),
        }
        try:
            await self.db.create_role_menu(doc)
        except Exception as e:
            logger.error(f"Menu pubblicato ma non salvato (si fermerà al riavvio): {e}")
        return {**doc, 'jump_url': message.jump_url}

    @app_commands.command(name="create-role-menu", description="Create a role menu (Admin)")
    @app_commands.describe(
        title="Title of the role menu",
        description="Description of the role menu",
        role1="First role",
        role2="Second role (optional)",
        role3="Third role (optional)",
        role4="Fourth role (optional)",
        role5="Fifth role (optional)",
        role6="Sixth role (optional)",
        role7="Seventh role (optional)",
        role8="Eighth role (optional)",
        role9="Ninth role (optional)",
        role10="Tenth role (optional)",
        exclusive="Can users only pick ONE role? (yes/no)",
        channel="Channel to send menu (optional)"
    )
    @is_admin()
    async def create_role_menu(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        role1: discord.Role,
        exclusive: str,
        role2: Optional[discord.Role] = None,
        role3: Optional[discord.Role] = None,
        role4: Optional[discord.Role] = None,
        role5: Optional[discord.Role] = None,
        role6: Optional[discord.Role] = None,
        role7: Optional[discord.Role] = None,
        role8: Optional[discord.Role] = None,
        role9: Optional[discord.Role] = None,
        role10: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None
    ):
        """Create role menu directly with slash command"""
        target_channel = channel or interaction.channel
        is_exclusive = exclusive.lower() in ['yes', 'y', 'true']
        
        # Collect all roles
        roles = [role1]
        if role2:
            roles.append(role2)
        if role3:
            roles.append(role3)
        if role4:
            roles.append(role4)
        if role5:
            roles.append(role5)
        if role6:
            roles.append(role6)
        if role7:
            roles.append(role7)
        if role8:
            roles.append(role8)
        if role9:
            roles.append(role9)
        if role10:
            roles.append(role10)
        
        # Build role list
        role_list = []
        for role in roles:
            if role.is_default() or role.is_integration():
                continue
            
            role_emoji = None
            if role.unicode_emoji:
                role_emoji = role.unicode_emoji
            
            role_list.append({
                'role': role,
                'emoji': role_emoji or "🎭",
                'label': role.name
            })
        
        if not role_list:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Valid Roles", "Please select valid roles."),
                ephemeral=True
            )
            return
        
        await self.post_menu(target_channel, title, description, role_list, is_exclusive, interaction.user.id)

        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Role Menu Created!",
                f"{'Exclusive' if is_exclusive else 'Multi-select'} role menu created in {target_channel.mention}"
            ),
            ephemeral=True
        )
        
        logger.info(f"Role menu created by {interaction.user} with {len(role_list)} roles")

    @app_commands.command(name="addrole", description="Add a role to a user (Admin)")
    @app_commands.describe(user="User to add role to", role="Role to add")
    @is_admin()
    async def add_role(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        """Add role to user"""
        if role in user.roles:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Already Has Role", f"{user.mention} already has {role.mention}"),
                ephemeral=True
            )
            return

        try:
            await user.add_roles(role)
            embed = EmbedFactory.success("Role Added", f"Added {role.mention} to {user.mention}")
            await interaction.response.send_message(embed=embed)
            logger.info(f"{interaction.user} added role {role} to {user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to manage roles"),
                ephemeral=True
            )

    @app_commands.command(name="removerole", description="Remove a role from a user (Admin)")
    @app_commands.describe(user="User to remove role from", role="Role to remove")
    @is_admin()
    async def remove_role(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        """Remove role from user"""
        if role not in user.roles:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Doesn't Have Role", f"{user.mention} doesn't have {role.mention}"),
                ephemeral=True
            )
            return

        try:
            await user.remove_roles(role)
            embed = EmbedFactory.success("Role Removed", f"Removed {role.mention} from {user.mention}")
            await interaction.response.send_message(embed=embed)
            logger.info(f"{interaction.user} removed role {role} from {user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to manage roles"),
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Roles(bot, bot.db, bot.config))
