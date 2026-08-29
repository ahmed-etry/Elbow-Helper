from __future__ import annotations

from dataclasses import dataclass

from elbow_helper.configuration.roles import CORE, CWL_HELPERS, LEAD, LEAD_PLUS, RECRUITERS

CAT_GENERAL = "General"
CAT_ECONOMY = "Achievements & Economy"
CAT_RECRUITMENT = "Recruitment"
CAT_CWL = "CWL"
CAT_ROSTERS = "Rosters"
CAT_REPORTS = "Reports & Exports"
CAT_EVENTS = "Events"
CAT_WAR = "War"
CAT_SUPPORT = "Support & Access"
CAT_ADMIN = "Admin & Debug"

CATEGORY_ORDER: tuple[str, ...] = (
    CAT_GENERAL,
    CAT_ECONOMY,
    CAT_RECRUITMENT,
    CAT_CWL,
    CAT_ROSTERS,
    CAT_REPORTS,
    CAT_EVENTS,
    CAT_WAR,
    CAT_SUPPORT,
    CAT_ADMIN,
)

MANAGEMENT_GUIDE_URL = "https://discord.com/channels/1063032179011096597/1266325899003957350/1450734486504603864"
CWL_GUIDE_URL = "https://discord.com/channels/1063032179011096597/1450771314494406687/1450771317857980508"
RECRUITMENT_GUIDE_URL = "https://discord.com/channels/1063032179011096597/1450766981442568203/1450766984860925952"

MANAGEMENT_GUIDE_NOTE = f"📄 [Management guide]({MANAGEMENT_GUIDE_URL})"
CWL_GUIDE_NOTE = f"📄 [CWL guide]({CWL_GUIDE_URL})"
RECRUITMENT_GUIDE_NOTE = f"📄 [Recruitment guide]({RECRUITMENT_GUIDE_URL})"


@dataclass(frozen=True)
class HelpEntry:
    path: str
    summary: str
    details: str
    category: str
    visible_to: frozenset[int] | None = None
    examples: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


HELP_ENTRIES: tuple[HelpEntry, ...] = (
    HelpEntry(
        path="/help",
        summary="Browse commands or get help using one.",
        details=(
            "Run `/help` without choosing a command to browse commands available to you, "
            "or choose a command to see how to use it."
        ),
        category=CAT_GENERAL,
        examples=("/help", "/help command:/plan"),
    ),
    HelpEntry(
        path="/ping",
        summary="Check the bot's response time.",
        details="Shows the bot's current response time in milliseconds.",
        category=CAT_GENERAL,
    ),
    HelpEntry(
        path="/plan",
        summary="Get help planning an attack.",
        details=(
            "Share a base and ask for an attack plan that fits your army levels and preferred strategies. "
            "The selected Clash account supplies the army levels shown with the request."
        ),
        category=CAT_GENERAL,
        examples=("/plan player:#PLAYER strategies:Hydra into core base_image:image",),
    ),
    HelpEntry(
        path="/transfer request",
        summary="Ask to move to another family clan.",
        details="Adds you to the selected family clan's transfer queue.",
        category=CAT_GENERAL,
        examples=("/transfer request destination:BEH",),
        notes=("Pings the elders for that clan so someone can accept you in-game.",),
    ),
    HelpEntry(
        path="/transfer cancel",
        summary="Cancel your transfer request.",
        details="Removes your pending request for the selected family clan from its transfer queue.",
        category=CAT_GENERAL,
        examples=("/transfer cancel destination:BEH",),
    ),
    HelpEntry(
        path="/achievements",
        summary="Check achievements, progress, and rewards.",
        details=(
            "Shows completed achievements, progress toward unfinished achievements, and their coin rewards. "
            "Leave the member option empty to view your own achievements."
        ),
        category=CAT_ECONOMY,
        examples=("/achievements", "/achievements member:@User"),
    ),
    HelpEntry(
        path="/achievement leaderboard",
        summary="See which members have earned the most achievements.",
        details="Shows the top 10 members outside leadership, ranked by completed achievements.",
        category=CAT_ECONOMY,
    ),
    HelpEntry(
        path="/inventory",
        summary="Check your coins and raffle ticket.",
        details=(
            "Shows your coin balance and whether you have a raffle ticket this month. "
            "Leadership can choose another member to view their inventory."
        ),
        category=CAT_ECONOMY,
        examples=("/inventory", "/inventory user:@User"),
    ),
    HelpEntry(
        path="/economyinfo",
        summary="See how coins, tickets, and rewards work.",
        details=(
            "Explains how members earn and spend coins, how monthly raffle tickets work, "
            "and this month's prize if one is set."
        ),
        category=CAT_ECONOMY,
    ),
    HelpEntry(
        path="/grant coins",
        summary="Add coins to a member's balance.",
        details=(
            "Adds a CWL or encouragement award to a member's coin balance. "
            "The reason appears in their coin history."
        ),
        category=CAT_ECONOMY,
        visible_to=LEAD_PLUS,
        examples=(
            "/grant coins user:@User category:cwl amount:5 reason:Top CWL performance",
            "/grant coins user:@User category:encouragement amount:3 reason:Great improvement",
        ),
        notes=(MANAGEMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/grant ticket",
        summary="Give a raffle ticket to a member.",
        details=(
            "Gives a member this month's raffle ticket without charging coins. "
            "A member can hold one ticket per month, and leadership cannot receive one."
        ),
        category=CAT_ECONOMY,
        visible_to=LEAD_PLUS,
        examples=("/grant ticket user:@User reason:Special contribution",),
        notes=(MANAGEMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/coinlog",
        summary="Check a member's coin history.",
        details=(
            "Shows a member's latest coin earnings and spending, including each amount, reason, "
            "and who made the change."
        ),
        category=CAT_ECONOMY,
        visible_to=CORE,
        examples=("/coinlog user:@User",),
    ),
    HelpEntry(
        path="/raffle list",
        summary="See who has raffle tickets right now.",
        details="Shows every member holding a raffle ticket for the current month.",
        category=CAT_ECONOMY,
        visible_to=LEAD_PLUS,
        notes=(MANAGEMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/raffle draw",
        summary="Draw raffle winners for this month or an earlier month.",
        details=(
            "Draws winners from eligible tickets for the selected month and announces the result in the channel. "
            "Leave the month empty for the current month, or enter an earlier month as YYYY-MM."
        ),
        category=CAT_ECONOMY,
        visible_to=LEAD_PLUS,
        examples=("/raffle draw", "/raffle draw month:2025-09"),
        notes=(MANAGEMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/raffle reroll",
        summary="Draw this month's raffle winners again.",
        details="Runs this month's raffle draw again and announces the result.",
        category=CAT_ECONOMY,
        visible_to=LEAD_PLUS,
        examples=("/raffle reroll",),
        notes=(MANAGEMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/raffle history",
        summary="See the raffle prize and winners for a selected month.",
        details=(
            "Shows the selected month's prize and winners. Leave the month empty to view the current month."
        ),
        category=CAT_ECONOMY,
        visible_to=LEAD_PLUS,
        examples=("/raffle history", "/raffle history month:2025-09"),
        notes=(MANAGEMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/raffle prize",
        summary="Choose the prize shown in this month's raffle posts.",
        details="Sets the prize and number of winners used in this month's raffle posts and draw.",
        category=CAT_ECONOMY,
        visible_to=CORE,
        examples=(
            "/raffle prize prize:Gold pass",
            "/raffle prize prize:Gold pass winners:3",
        ),
    ),
    HelpEntry(
        path="/raffle remove",
        summary="Remove a member's raffle ticket.",
        details="Removes a member's raffle ticket for the current month so they no longer appear as entered.",
        category=CAT_ECONOMY,
        visible_to=CORE,
        examples=("/raffle remove user:@User",),
    ),
    HelpEntry(
        path="/raffle clear",
        summary="Clear this month's raffle winners.",
        details=(
            "Clears this month's raffle winners. Use `clear_tickets:true` to also remove every ticket "
            "and let members enter again."
        ),
        category=CAT_ECONOMY,
        visible_to=CORE,
        examples=("/raffle clear", "/raffle clear clear_tickets:true"),
    ),
    HelpEntry(
        path="/achievement award",
        summary="Give an achievement to a member.",
        details=(
            "Adds the selected achievement and its coin reward to a member's profile. "
            "Set `silent` to true to skip the public achievement announcement."
        ),
        category=CAT_ECONOMY,
        visible_to=LEAD,
        examples=(
            "/achievement award user:@User achievement:Fresh Recruit",
            "/achievement award user:@User achievement:Fresh Recruit silent:true",
        ),
    ),
    HelpEntry(
        path="/achievement remove",
        summary="Remove an achievement from a member.",
        details="Removes the selected achievement from a member's profile and reverses any coin reward granted with it.",
        category=CAT_ECONOMY,
        visible_to=LEAD,
        examples=("/achievement remove user:@User achievement:Fresh Recruit",),
    ),
    HelpEntry(
        path="/accept",
        summary="Accept an applicant, link their accounts, and start their trial.",
        details=(
            "Shows a confirmation before setting the applicant's nickname and roles, linking their Clash accounts, "
            "posting the welcome message, and starting their trial."
        ),
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | LEAD,
        examples=("/accept applicant:@User clans:BEH BE4 nickname:Player player_tags:#ABC123 #DEF456 days:7",),
        notes=(
            "Run this in the applicant's ticket, or choose another ticket with the channel option.",
            "You must provide and confirm the player tags before accepting the applicant.",
            RECRUITMENT_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/account add",
        summary="Link one or more Clash accounts to a Discord member.",
        details=(
            "Links the supplied player tags to the selected Discord member. "
            "If an account is linked to someone else, the link moves to the selected member."
        ),
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | CORE,
        examples=("/account add member:@User tags:#ABC123 #DEF456",),
        notes=(RECRUITMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/account remove",
        summary="Unlink one or more Clash accounts.",
        details="Removes the Discord member link from each supplied player tag.",
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | CORE,
        examples=("/account remove tags:#ABC123 #DEF456",),
        notes=(RECRUITMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/account list",
        summary="Find who an account is linked to, or see a member's linked accounts.",
        details=(
            "Choose a Discord member to see their linked Clash accounts, or enter a player tag "
            "to find the Discord member linked to that account."
        ),
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | CORE,
        examples=("/account list member:@User", "/account list tag:#ABC123"),
        notes=(RECRUITMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/checkup",
        summary="Start the recruitment conversation with an applicant and include any remaining setup steps.",
        details=(
            "Starts the recruitment conversation in the applicant's ticket, asks them to read the rules, "
            "and includes any steps for linking their accounts or choosing their age and region roles."
        ),
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | CORE,
        examples=(
            "/checkup applicant:@User account_linked:true channel:#ticket",
            "/checkup applicant:@User account_linked:false channel:#ticket additional_notes:Link every Clash account",
        ),
        notes=(
            "Run this in the applicant's ticket, or choose another ticket with the channel option.",
            RECRUITMENT_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/finalize",
        summary="End a recruit's trial and give them full member access.",
        details=(
            "Ends the recruit's trial, replaces trial access with member access, updates the ticket, "
            "and posts the trial-ending message."
        ),
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | CORE,
        examples=("/finalize applicant:@User channel:#ticket",),
        notes=(
            "Run this in the applicant's ticket, or choose another ticket with the channel option.",
            RECRUITMENT_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/decline",
        summary="Decline an applicant and send the decision.",
        details=(
            "Posts the decline message in the applicant's ticket and marks the ticket as declined. "
            "Use additional notes when the standard message needs more context."
        ),
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | CORE,
        examples=("/decline applicant:@User channel:#ticket additional_notes:Not a fit",),
        notes=(
            "Run this in the applicant's ticket, or choose another ticket with the channel option.",
            RECRUITMENT_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/opinion",
        summary="Get an AI second opinion on an applicant ticket.",
        details="Uses the application and conversation in the selected applicant ticket to give an AI second opinion.",
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | CORE,
        examples=("/opinion ticket:#ticket",),
        notes=(RECRUITMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/recstatements",
        summary="Send an Under 16 or Hyperactive message to an applicant.",
        details=(
            "Posts the selected recruitment message in the applicant's ticket. "
            "Use additional notes when the standard message needs more context."
        ),
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | CORE,
        examples=(
            "/recstatements message:Under 16 applicant:@User",
            "/recstatements message:Hyperactive applicant:@User channel:#ticket additional_notes:Please read this before continuing",
        ),
    ),
    HelpEntry(
        path="/recstats",
        summary="See how many applicants came from each recruitment source this week.",
        details=(
            "Posts this week's applicant counts by recruitment source in the current channel. "
            "The post is removed after 24 hours."
        ),
        category=CAT_RECRUITMENT,
        visible_to=RECRUITERS | CORE,
    ),
    HelpEntry(
        path="/roster announcement",
        summary="Share CWL roster and transfer deadlines with the clan family.",
        details=(
            "Builds the CWL roster announcement for the clan family using the selected deadlines and timezone. "
            "Use preview to check it privately; otherwise, it posts in the CWL transfer channel."
        ),
        category=CAT_CWL,
        visible_to=LEAD,
        examples=(
            "/roster announcement deadline_mode:Single deadline for all deadline:01-20:00 timezone:UTC+01:00 - Paris",
            "/roster announcement deadline_mode:Main deadline + extra time for some clans deadline:01-20:00 timezone:UTC+01:00 - Paris delayed_deadline:02-20:00",
            "/roster announcement deadline_mode:Single deadline for all deadline:01-20:00 timezone:UTC+01:00 - Paris preview:true",
        ),
        notes=(
            "Example: deadline:01-20:00 means the 1st day of the month at 20:00 in the selected timezone, not 1 day and 20 hours from now.",
            "Double-check the deadline and timezone before posting.",
            "Posting also enables **Where Am I Playing?** and **CWL Channels**. They lock again when the CWL rosters open for the next month.",
            CWL_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/roster create",
        summary="Set up a roster members can join with linked Clash accounts.",
        details=(
            "Choose whether the roster is for the full clan family or a single clan. "
            "The default allows 500 accounts. Choose a signup role only when members should "
            "receive one while signed up."
        ),
        category=CAT_ROSTERS,
        visible_to=LEAD_PLUS,
        examples=(
            "/roster create name:CWL Sign-up clan:Full clan family signup_role:@Joining CWL",
        ),
    ),
    HelpEntry(
        path="/roster edit",
        summary="Update a roster's name, clan, signup role, Town Hall minimum, or account limit.",
        details=(
            "Changes the roster name, clan, signup role, Town Hall minimum, or account limit. "
            "Enter `0` as the Town Hall minimum when every level can sign up. "
            "Timing is managed separately with `/roster timing` and `/roster schedule`."
        ),
        category=CAT_ROSTERS,
        visible_to=LEAD_PLUS,
        examples=(
            "/roster edit roster:CWL Sign-up min_townhall:8",
            "/roster edit roster:CWL Sign-up min_townhall:0",
        ),
        notes=(
            "The Town Hall minimum applies to member signups. Leadership can add exceptions.",
            "Changing the minimum does not remove accounts already on the roster.",
        ),
    ),
    HelpEntry(
        path="/roster timing",
        summary="Set one opening and closing window for a roster.",
        details=(
            "Enter full opening and closing dates and times, then choose the timezone used for "
            "both. Leave both date-and-time fields blank to clear the window. The timing remains "
            "active whether or not the roster has been posted. Use "
            "`/roster schedule` instead for a monthly schedule. The CWL signup roster also uses "
            "this window for its announcement and reminders."
        ),
        category=CAT_ROSTERS,
        visible_to=LEAD_PLUS,
        examples=(
            "/roster timing roster:CWL Sign-up opens_on:2026-07-20 09:00 "
            "closes_on:2026-07-30 21:00 timezone:Asia/Beirut",
        ),
    ),
    HelpEntry(
        path="/roster schedule",
        summary="Set automatic monthly opening and closing times for a roster.",
        details=(
            "Choose day `1`–`28`, `last` for the final day, `last-1` for the day before, or "
            "`last-2` for two days before. Use the month-end options instead of `29`–`31`, since "
            "shorter months do not have those dates. If the closing date and time is the same as "
            "or before the opening, it falls "
            "in the following month. Select the timezone for the times you enter. Monthly schedules "
            "keep the same UTC time. If local clocks later change for daylight saving, the roster's "
            "local opening and closing times can shift."
        ),
        category=CAT_ROSTERS,
        visible_to=LEAD_PLUS,
        examples=(
            "/roster schedule roster:CWL Sign-up open_day:last-2 open_time:11:00 close_day:last-1 close_time:20:00 timezone:Europe/Paris",
            "/roster schedule roster:CWL Sign-up open_day:last-1 open_time:11:00 close_day:2 close_time:20:00 timezone:Europe/Paris",
            "/roster schedule roster:CWL Sign-up enabled:false",
        ),
        notes=(
            "The first schedule needs opening and closing days and times, plus a timezone. After that, you can change only the fields you need.",
            "The CWL signup roster also schedules its announcement and reminders from this window.",
            "The schedule can be enabled before posting and applies wherever the roster is posted.",
        ),
    ),
    HelpEntry(
        path="/roster post",
        summary="Post a roster in this channel.",
        details=(
            "Posts the roster with Signup, Opt-out, refresh, and a gear menu. Every post of the "
            "same roster stays in sync. The gear menu handles account changes, signup controls, "
            "Google Sheets exports, and the Discord layout. Larger rosters add page controls. The "
            "message shows the signup role, account limit, Town Hall minimum when set, and signup "
            "timing while signup controls are shown."
        ),
        category=CAT_ROSTERS,
        visible_to=LEAD_PLUS,
        examples=("/roster post roster:CWL Sign-up",),
        notes=(
            "The Player column is always shown. Town Hall, Discord username, and Clan can be hidden.",
            "Player and Discord username length settings affect Discord only; Google Sheets keeps every column.",
        ),
    ),
    HelpEntry(
        path="/roster export",
        summary="Export current roster signups to Google Sheets.",
        details=(
            "Exports each signed-up Clash account with its current name, clan, Town Hall, "
            "combined hero level, player tag, Discord member, and signup time. Every export "
            "creates a new spreadsheet that can be opened in Google Sheets or downloaded."
        ),
        category=CAT_ROSTERS,
        visible_to=LEAD_PLUS,
        examples=("/roster export roster:CWL Sign-up",),
    ),
    HelpEntry(
        path="/roster list",
        summary="See current rosters and their timing.",
        details=(
            "Shows each roster's status, account limit, Town Hall minimum, and "
            "one-off or monthly timing."
        ),
        category=CAT_ROSTERS,
        visible_to=LEAD_PLUS,
    ),
    HelpEntry(
        path="/roster clone",
        summary="Create a roster using another roster's settings.",
        details=(
            "Copies the clan, signup role, account limit, Town Hall minimum, monthly schedule, "
            "signup controls, and roster layout. The clan, role, and limits can be changed while "
            "cloning; enter `0` for no Town Hall minimum. The new roster starts closed with no "
            "signups, posts, one-off timing, or Google Sheet."
        ),
        category=CAT_ROSTERS,
        visible_to=LEAD_PLUS,
        examples=(
            "/roster clone roster:CWL Sign-up name:War Sign-up",
            "/roster clone roster:BEH CWL name:BE4 CWL clan:BE4 signup_role:@BE4 CWL max_members:30 min_townhall:16",
        ),
    ),
    HelpEntry(
        path="/roster delete",
        summary="Permanently remove a roster and its signup history.",
        details="Asks for confirmation before deleting the roster and removing its signup role from members with current signups.",
        category=CAT_ROSTERS,
        visible_to=LEAD_PLUS,
        examples=("/roster delete roster:Old Sign-up",),
    ),
    HelpEntry(
        path="/cwl brief",
        summary="Post the clan's CWL rules, rotations, and leadership details.",
        details=(
            "Builds and posts the selected CWL brief in the clan's CWL info channel. "
            "Choose the mode for that clan's CWL approach and whether the brief should include daily rotations."
        ),
        category=CAT_CWL,
        visible_to=LEAD_PLUS | CWL_HELPERS,
        examples=(
            "/cwl brief clan:BEH mode:Highly Motivated helper_cwl:@User rotations:true lead_cwl:@User",
            "/cwl brief clan:BEC mode:Mainline Maintain helper_cwl:@User rotations:false",
        ),
        notes=(
            "Choose the mode that matches how that clan is handling this CWL.",
            CWL_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/transfer reminder",
        summary="Notify members who still need to move to their CWL clan.",
        details=(
            "Checks each CWL roster against the players' current clans and posts only when transfers remain. "
            "Clans that have started CWL are left out automatically."
        ),
        category=CAT_CWL,
        visible_to=LEAD_PLUS | CWL_HELPERS,
        examples=("/transfer reminder", "/transfer reminder exclude:BEH BEP"),
        notes=(CWL_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/cwl cc",
        summary="Record the Clan Castle status for a CWL day.",
        details=(
            "Updates the selected CWL day's Clan Castle status and refreshes the status post "
            "in the clan's registered CWL thread."
        ),
        category=CAT_CWL,
        visible_to=CWL_HELPERS | LEAD_PLUS,
        examples=("/cwl cc day:Day 3 status:Filled",),
        notes=(
            "📍 Run this inside a registered CWL thread.",
            CWL_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/cwl bonus",
        summary="Build a spreadsheet to help assign CWL bonus medals.",
        details=(
            "Builds a Google Sheet and Excel workbook comparing eligible players for one clan "
            "or the full family. Choose a completed season, or leave it empty to use the latest available season."
        ),
        category=CAT_CWL,
        visible_to=LEAD_PLUS | CWL_HELPERS,
        examples=(
            "/cwl bonus clan:ALL",
            "/cwl bonus clan:BEH season:2025-09",
        ),
        notes=(
            "⚠️ This is an estimate, not an automatic final decision.",
            CWL_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/cwl roster",
        summary="Build a workbook to help assign CWL signups to clans.",
        details=(
            "Builds a roster-planning workbook from current signups, linked Clash accounts, completed CWL performance, "
            "and leadership records. Choose how many completed seasons to include."
        ),
        category=CAT_CWL,
        visible_to=LEAD_PLUS,
        examples=(
            "/cwl roster",
            "/cwl roster history:Latest 6 seasons",
        ),
        notes=(
            "Only current signups are shown, but rankings include everyone who was on that clan's roster for the season.",
            "**Season History** shows the clan and league recorded for each completed CWL season.",
            "Clan assignments remain leadership decisions and can be edited in the Google Sheet.",
            CWL_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/cwl register",
        summary="Connect a clan's CWL thread to its status updates.",
        details=(
            "Links the selected clan to an existing CWL discussion thread and adds its status post. "
            "Copy the thread ID from its URL; linking a new thread replaces that clan's previous one."
        ),
        category=CAT_CWL,
        visible_to=CORE,
        examples=("/cwl register clan:Hellbow thread_id:1234567890",),
    ),
    HelpEntry(
        path="/record add",
        summary="Document an incident involving a member.",
        details=(
            "Creates a leadership record for the selected member, including the incident category and type, "
            "what happened, and any context leadership should know."
        ),
        category=CAT_REPORTS,
        visible_to=LEAD_PLUS,
        examples=(
            "/record add user:@User category:CWL type:Missed Attack note:Missed the final attack after confirming availability",
        ),
    ),
    HelpEntry(
        path="/record export",
        summary="Download all leadership records or one member's records.",
        details=(
            "Creates a downloadable spreadsheet containing every leadership record, "
            "or only the selected member's records."
        ),
        category=CAT_REPORTS,
        visible_to=LEAD_PLUS,
        examples=("/record export", "/record export user:@User"),
    ),
    HelpEntry(
        path="/record edit",
        summary="Update the category, type, or details of a member's record.",
        details=(
            "Opens controls for choosing one of the member's recent records and changing "
            "its category, incident type, or details."
        ),
        category=CAT_REPORTS,
        visible_to=LEAD_PLUS,
        examples=("/record edit user:@User",),
    ),
    HelpEntry(
        path="/record remove",
        summary="Delete one of a member's incident records.",
        details="Removes the selected incident record from the member's leadership records.",
        category=CAT_REPORTS,
        visible_to=LEAD_PLUS,
        examples=("/record remove user:@User record:#12",),
    ),
    HelpEntry(
        path="/health player",
        summary="Export a health report for a Clash account.",
        details=(
            "Creates a downloadable health report for a Clash account from any family clan roster. "
            "Choose a report period, or use Custom dates to enter your own start and end dates."
        ),
        category=CAT_REPORTS,
        visible_to=LEAD_PLUS,
        examples=(
            "/health player account:#PLAYER",
            "/health player account:#PLAYER period:Last 14 days",
            "/health player account:#PLAYER period:Custom dates",
        ),
        notes=(
            "The period defaults to the last 30 days.",
            MANAGEMENT_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/health clan",
        summary="Export a clan or family health report.",
        details=(
            "Creates a downloadable health report for one clan or the full family. "
            "Choose a report period, or use Custom dates to enter your own start and end dates."
        ),
        category=CAT_REPORTS,
        visible_to=LEAD_PLUS,
        examples=(
            "/health clan clan:BE1 period:Last 14 days",
            "/health clan clan:ALL",
            "/health clan clan:ALL period:Custom dates",
        ),
        notes=(
            "The period defaults to the last 30 days.",
            MANAGEMENT_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/health settings",
        summary="Set the activity expectations used in a clan's health reports.",
        details=(
            "Opens the selected clan's settings panel to update the activity expectations "
            "used by both account and clan health reports."
        ),
        category=CAT_REPORTS,
        visible_to=LEAD_PLUS,
        examples=("/health settings clan:BE1",),
        notes=(
            "Run a new health report after saving if you want to review the updated results.",
            MANAGEMENT_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/event panel",
        summary="Manage event schedules and participation trackers.",
        details=(
            "Opens the event controls, where you can create one-time events and edit, reorder, enable, "
            "disable, reset, or remove trackers."
        ),
        category=CAT_EVENTS,
        visible_to=LEAD,
        notes=(MANAGEMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/event list",
        summary="See every event tracker and its current status.",
        details=(
            "Lists every event tracker and its current status, "
            "with controls for viewing or managing each one."
        ),
        category=CAT_EVENTS,
        visible_to=LEAD,
    ),
    HelpEntry(
        path="/event update",
        summary="Update every event count now.",
        details=(
            "Refreshes every event tracker immediately instead of waiting for the next automatic update."
        ),
        category=CAT_EVENTS,
        visible_to=LEAD,
    ),
    HelpEntry(
        path="/warstatement one-attack-missed",
        summary="Ask players why they used only one war attack.",
        details=(
            "Posts the standard follow-up asking the selected players why they used only one attack. "
            "Choose the clan where it should be posted and add notes if needed."
        ),
        category=CAT_WAR,
        visible_to=LEAD_PLUS,
        examples=("/warstatement one-attack-missed clan:BEH players:@User @User",),
    ),
    HelpEntry(
        path="/warstatement missed-attacks",
        summary="Tell players they were removed from war after missing both attacks.",
        details=(
            "Posts the standard follow-up telling the selected players they were removed from war after missing both attacks. "
            "Choose the clan where it should be posted and add notes if needed."
        ),
        category=CAT_WAR,
        visible_to=LEAD_PLUS,
        examples=("/warstatement missed-attacks clan:BEH players:@User @User",),
    ),
    HelpEntry(
        path="/warstatement first-claim",
        summary="Ask a player why they attacked someone else's first claim.",
        details=(
            "Posts the standard follow-up asking the attacker why they used the other player's first claim. "
            "Choose the clan where it should be posted and add notes if needed."
        ),
        category=CAT_WAR,
        visible_to=LEAD_PLUS,
        examples=(
            "/warstatement first-claim clan:BEH victim:@User attacker:@User",
            "/warstatement first-claim clan:BEH victim:@User attacker:@User notes:Please check claims before attacking",
        ),
    ),
    HelpEntry(
        path="/warstatement breaking-rules",
        summary="Tell players they were removed from war for rule or communication problems.",
        details=(
            "Posts the standard follow-up telling the selected players they were removed from war for rule or communication "
            "problems. Choose the clan where it should be posted and add notes if needed."
        ),
        category=CAT_WAR,
        visible_to=LEAD_PLUS,
        examples=("/warstatement breaking-rules clan:BEH players:@User @User",),
    ),
    HelpEntry(
        path="/warstatement war-filler",
        summary="Tell war fillers why they were added and how future war selection works.",
        details=(
            "Posts the standard follow-up explaining why the selected players were added as war fillers and how future "
            "war selection works. Choose the clan where it should be posted and add notes if needed."
        ),
        category=CAT_WAR,
        visible_to=LEAD_PLUS,
        examples=("/warstatement war-filler clan:BEH players:@User @User",),
    ),
    HelpEntry(
        path="/open",
        summary="Open a support ticket for a member.",
        details="Creates a private support ticket for the selected member about the topic you provide.",
        category=CAT_SUPPORT,
        visible_to=LEAD,
        examples=("/open user:@User topic:War discussion",),
        notes=(MANAGEMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/close",
        summary="Close the current ticket.",
        details=(
            "Closes the current support ticket, locks the member's messaging access, "
            "and saves the transcript."
        ),
        category=CAT_SUPPORT,
        visible_to=LEAD | RECRUITERS,
        notes=(
            "📍 Run this inside the ticket you want to close.",
            "You can still reopen or delete the ticket afterwards.",
            MANAGEMENT_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/hibernate",
        summary="Save a member's roles and move them into hibernation.",
        details="Saves the member's current roles, moves them into hibernation, and sends instructions for returning later.",
        category=CAT_SUPPORT,
        visible_to=LEAD_PLUS,
        examples=("/hibernate user:@User",),
        notes=(
            "⚠️ Previous rank roles, such as Elder, aren't restored automatically.",
            MANAGEMENT_GUIDE_NOTE,
        ),
    ),
    HelpEntry(
        path="/reactivate",
        summary="Restore your saved roles and return from hibernation.",
        details=(
            "Restores your saved roles and opens a ticket to help you settle back in. "
            "Leadership can choose another member to reactivate."
        ),
        category=CAT_SUPPORT,
        examples=("/reactivate", "/reactivate user:@User"),
    ),
    HelpEntry(
        path="/connections",
        summary="Manage rules that add or remove member roles.",
        details=(
            "Posts the role connections board in this channel. Rules can add or remove roles "
            "based on other roles a member has or lacks."
        ),
        category=CAT_SUPPORT,
        visible_to=LEAD,
        notes=(MANAGEMENT_GUIDE_NOTE,),
    ),
    HelpEntry(
        path="/api",
        summary="Check Clash API status for a clan.",
        details=(
            "Tests the Clash API connection for the selected clan and reports its status and current-war data, "
            "along with response time and rate-limit details when available."
        ),
        category=CAT_ADMIN,
        visible_to=CORE,
        examples=("/api clan:BEH",),
    ),
)


HELP_INDEX = {entry.path: entry for entry in HELP_ENTRIES}
