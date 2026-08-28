"""Recruitment statement templates used by slash commands."""

from __future__ import annotations

ACCEPT_TEMPLATE = (
    "Welcome in {user_mention}! <:chadHog:1217151645297672322>\n\n"
    "Everything looks good. You will now be placed on trial for around **{trial_length}** so we can make sure "
    "the clan is a good fit both ways.\n\n"
    "During that trial period, you will be expected to participate in wars. We may look at things like "
    "your war attacks, communication, participation, rule-following, and whether the placement makes sense. "
    "You may also receive feedback or be asked questions about your attacks during that process.\n\n"
    "If all goes well after that, you will be fully set up and ready to stay with us "
    "<:pray:1209861423963045928>\n\n"
    "I will link below the clan(s) where you are welcome to join <:stonks:1209861452492709938>\n\n"
    "# Key points:\n\n"
    "- Read through {info_channels_text} and <#{server_rules}> carefully to understand how things work and "
    "what is expected of you.\n\n"
    "- Write **\"My Elbows have been Browned\"** in your join request(s) to get accepted.\n\n"
    "- This ticket will be kept up for the duration of the trial, so feel free to ask any technical "
    "questions you might have.\n\n"
    "Hope you like it here <:happy:1217139492176007240>\n\n"
    "{clan_links}"
    "{additional_notes}"
)

DECLINE_TEMPLATE = (
    "Hey {user_mention},\n\n"
    "After reviewing your answers and the conversation, we've decided not to move forward with your application "
    "to the clan family <:pray:1209861423963045928>\n"
    "We wish you the best finding a clan that suits you.\n"
    "Clash on! <:chadHog:1217151645297672322>"
    "{additional_notes}"
)

CHECKUP_TEMPLATE = (
    "Hey {user_mention}\n\n"
    "Thanks for filling out the application!\n\n"
    "We'll start your application process here by chatting. Brown Elbow is a clan family made up of seven "
    "primary clans and two utility clans that we break out during CWL.\n"
    "With so many people to manage, we try our best to make sure each applicant is properly vetted before "
    "placing them in a clan.\n"
    "One of our main goals is to make sure the Brown Elbow family stays a fun and competitive place for all "
    "members <:giga:1207390487284027502>\n\n"
    "Before we continue, please make sure you have read <#{server_rules}>.\n\n"
    "{account_link_line}"
    "{self_roles_line}"
    "{followup_question}\n\n"
    "After that, we'll have a few questions for you."
    "{additional_notes}"
)

FINALIZE_TEMPLATE = (
    "Great to hear, {user_mention} 😄\n"
    "Your trial is complete, and you now have full member access to the server "
    "<:pray:1209861423963045928>"
    "{additional_notes}"
)

RARE_STATEMENTS = {
    "under16": (
        "{user} Sadly your account might be listed as being under 16. Due to this, "
        "you can't join non-family friendly clans. This is a limitation set by "
        "Supercell and we have limited options\n\n"
        "Your options are:\n"
        "- Contacting support\n"
        "- Making a new account\n"
        "- Searching only for family friendly clans\n"
        "- Waiting to turn 16\n\n"
        "Best of luck\n"
        "Feel free to tag us if anything changes 😄"
    ),
    "hyperactive": (
        "{user}\n"
        "Just a heads up, we are competitive and active, but do realise your view "
        "of what that means might differ from ours at large.\n\n"
        "Not everyone has multiple accounts or even the ones that do, might not "
        "have so much time to attend their accounts.\n\n"
        "And in the end, everyone has their own real life to attend to and we don't "
        "want to make clash a job. You should be able to come to the clan, enjoy "
        "for the time that you have and have a pleasant environment."
    ),
}
