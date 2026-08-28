"""Long CWL text templates."""

from __future__ import annotations

SIGNUP_STATEMENT = """# CWL signups are open <@&1453392663222161532>!
## Sign up above and you will get <@&1209095400301133824>  and we will make sure to find you a spot (*at least your main*) <:giga:1207390487284027502> 
You will get assigned a clan to do CWL in that best suits your base development and skill <:stonks:1209861452492709938> 
*(You might need to move to a different clan for CWL)*
**Note** If you see <#1168637963526209546> that means you have signed up for CWL and here you will see your CWL clan when rosters are prepared 

# <@&1251809406572826634>  
Extra hands in CWL are always needed and can get reserved bonus medals if you do good work <:gigaPhone:1357320527248756817> 
## Apply in <#1404198684501086381>  "**🏅CWL Helper**" button
### We need help with (per clan):
1x <@&1179731225464803368>  : 
- Fill defensive CCs
- Offer attacking advice when needed
-  Act as a bridge to <@&1179731484546973826>  to keep things running smoothly

# Loot Boost
## Extra Wars are regular wars run during CWL, offering more **loot **and **ores** for participants <:letsgoooooo:1209861534080311386> 
If you're interested, then get the <@&1202327291477373028> role (*skip if you already have done this*)  by going to <#1368168775546830929>  and reacting to the "War specialist" embed by clicking the 🗡️ emoji. 
You will then gain access to the **Sign up** and  all the **details ** here: https://discord.com/channels/1063032179011096597/1215398437504294932
"""

FIRST_REMINDER = """# <@&1453392663222161532> Last few days to sign up for CWL <:giga:1207390487284027502> 

Thanks to those who already opened a ticket for <@&1251809406572826634> <:hogGiga:1217147177063809054> 
"""

SECOND_REMINDER_TEMPLATE = """# <@&1453392663222161532> CWL starts soon! Sign up here if you want to play <:myman:1266848709417766976>
One signup covers every clan. We'll build all clan rosters from this list and do our best to include everyone who signs up <:salute:1310280565034848257>

## A place is not guaranteed after signups close. <:careful:1328076913041739887>
Check the earlier messages for full details. <:pray:1209861423963045928>
"""

ROSTER_SINGLE_DEADLINE_SECTION = """## When do I need to move?
Move as early as you can so your CWL clan is ready.
# ❗ All CWL spins are scheduled for {deadline}
*This time is shown in your local timezone.*

If you have not transferred by then, you may miss your assigned clan's CWL spin. There will be no deadline extension. <:careful:1328076913041739887>
"""

ROSTER_DELAYED_DEADLINE_SECTION = """## When do I need to move?
Move as early as you can so your CWL clan is ready.
# ❗ The main CWL spin is scheduled for {deadline}
*This time is shown in your local timezone.*

For <@&1142803352971923546>, <@&1290635745903513632>, <@&1224076742562549870>, <@&1324005097352462336>, and <@&1356441270427324516>: if the clan is still waiting on transfers at the main deadline, its CWL spin will move to {delayed_deadline}.
There will be no further extension. <:painGiant:1357359283918082069>

Members in <@&1142795583959089243> and <@&1356447585954566339> who miss the main deadline will miss CWL. <:careful:1328076913041739887>
"""

ROSTER_TEMPLATE = """# <@&1209095400301133824> Rosters are posted! <:giga:1207390487284027502>
{intro_text}

# How to transfer faster and easier?
Use the command 
> /transfer request
And choose the short clan tag for your destination! 
**Do note**: After running `/transfer request`, you still need to send an in-game join request.  
That command only notifies elders to help speed up transfers.

Now where is your destination? <:maybe:1209121676093165578> 
Read on <:painMP:1389616272630878358> 👇 

## Where to find your transfer destination?

**a)** Use **Where Am I Playing?** under [**CWL Rosters and Transfers**]({hub_message_url}). It shows where you’re playing and whether any of your accounts still need to move.

Want to check every roster? Use **See All Rosters** there. The clan name and tag at the top of each roster link directly to that clan.

**b)** You can also check your roles under **Where I war** on your Discord profile. A role such as **CWL in BE4** means you’re playing in BE4.

{deadline_section}
## What happens next?
Your CWL role also gives you access to the #war-discussion and #clan-cwl channels for your assigned clan during CWL.

There will be additional reminders sent out for those who are yet to transfer. Please note that sometimes clans can be full due to uneven transfers, so don't be afraid to ping your clans CWL helpers if needed, namely one of these <:pray:1209861423963045928> 
<@&1251809893690638396>  <@&1251810411314020423>  <@&1356447591952552077>  <@&1290637470093934644>  <@&1278824489316122765>  <@&1251810035554582578>  <@&1356441275309494464>  

**Note**: You can join normal wars in addition to CWL! These wars will be run in a specific clan where there is no CWL going on, namely {war_specialist_role}. 
Check out up to date information about these extra wars in <#1215398437504294932> (if you don't have access, you should get this self role by clicking the reaction on the message https://discord.com/channels/1063032179011096597/1368168775546830929/1368217288188366882 )

### Let's get CWL rolling smoothly everyone <:letsgoooooo:1209861534080311386> <:hehe:1310735618367684751> <a:judoGigaLook:1310355325332754552>
"""

BRIEF_TEMPLATES = {
    "highly_motivated": """# Welcome to CWL, our dream team {cwl_team_role} <:giga:1207390487284027502>
This is a season of great motivation and great plans <:hogGiga:1217147177063809054> 

A special shoutout to <@&1210991797124202689>  for always coming through and helping us fill the roster with strong attackers when we need them most ⚔️ <:myman:1266848709417766976> 

## CWL leadership
CWL is very much a team effort, so we have a team which will help everything run smoothly <:pray:1209861423963045928> 
<@&1179731484546973826> {lead_cwl}, <@&1179731225464803368> {helper_cwl}
Quick summary:
<@&1179731484546973826> : Overall CWL support where necessary, planning ahead for the clan, morale and direction(, making roster rotations)
<@&1179731225464803368>: Your bridge to other members of our CWL team, helping you to be on track with attacks, filling our defensive cc's and there to give attacking advice if you need it.
Turn to our helpers if you need assistance <:maybe:1209121676093165578> <:stonks:1209861452492709938> 
## No custom daily plans 

That's all for now and let's have a great CWL of great development, attacks and most importantly, collaboration and teamwork <:giga:1207390487284027502> <:ima:1209863041353654344> <:keepGoing:1210560820006883419>
""",
    "mainline_pushing": """# Welcome to CWL {cwl_team_role} <:giga:1207390487284027502> 
CWL here will be nice and straightforward, so I will keep it short
# War plan <:maybe:1209121676093165578> 
The war plan is similar to ordinary war plan during the month, but for CWL there are simplifications to it.
Read "Rules about attacking in CWL" embed that is found in {clan_post_channel}

To keep execution sharp this month, follow this roadmap:

1) Use your attack!
The lines between promoting, staying, and demoting can get pretty thin. Not using an attack (which takes around 5 minutes) lets down the full roster and will land you on the bench.

2) Don't 1 Star!
Every attack needs a plan to secure at least 2 stars. 1-star attacks are what lose wars. Let the other team make the mistakes.

3) Ask questions and seek advice!
If you have a question or want advice, use /plan in <#1313939778994962523> and skilled players will help you out.

4) Use your potions!
CWL is the main competitive event for our clan. If you are signed up and are not maxed or nearly maxed offensively, you should be using potions.

Not following these rules, when CWL Leadership gets notified, will get you swapped out (until you explain yourself) of CWL.
Just keep good teamwork and sportsmanship and there should be no issues <:giga:1207390487284027502>

# Rotations
Notice how there are more players on the roster than there are spots (roster button, top of {clan_post_channel}) That means that there will be daily rotations where the worst or non-attacker will be rotated out with the person on the bench. Don't worry, we will aim to secure at least 8 stars for everyone such that you get the maximal CWL medals.
# CWL Leadership <:chadHog:1217151645297672322> 
We have a <@&1179731484546973826> so we picked the team who can best compete for a promotion <:hogGiga:1217147177063809054> 
<@&1179731484546973826> {lead_cwl}, <@&1179731225464803368> {helper_cwl}
Quick summary:
<@&1179731484546973826> : Overall CWL support where necessary, planning ahead for the clan, morale and direction(, making roster rotations)
<@&1179731225464803368>: Your bridge to other members of our CWL team, helping you to be on track with attacks, filling our defensive cc's and there to give attacking advice if you need it.
Turn to our helpers if you need assistance <:maybe:1209121676093165578> <:stonks:1209861452492709938> 

Let's have a nice and enjoyable CWL here <:stonks:1209861452492709938> <:ima:1209863041353654344>
""",
    "mainline_maintain": """# Welcome to CWL {cwl_team_role} <:giga:1207390487284027502> 
CWL here will be nice and straightforward, so I will keep it short
# War plan <:maybe:1209121676093165578> 
The war plan is similar to ordinary war plan during the month, but for CWL there are simplifications to it.
Read "Rules about attacking in CWL" embed that is found in {clan_post_channel}

To keep execution sharp this month, follow this roadmap:

1) Use your attack!
The lines between promoting, staying, and demoting can get pretty thin. Not using an attack (which takes around 5 minutes) lets down the full roster and will land you on the bench.

2) Don't 1 Star!
Every attack needs a plan to secure at least 2 stars. 1-star attacks are what lose wars. Let the other team make the mistakes.

3) Ask questions and seek advice!
If you have a question or want advice, use /plan in <#1313939778994962523> and skilled players will help you out.

4) Use your potions!
CWL is the main competitive event for our clan. If you are signed up and are not maxed or nearly maxed offensively, you should be using potions.

5) Try your best, and have fun.
We want to win, but this is still a game. Don't get too stressed out. That said, CWL performance is one factor (among many) in placement, promotions, elder status, etc., so always do your best.

Not following these rules, when CWL Leadership gets notified, will get you swapped out (until you explain yourself) of CWL.
Just keep good teamwork and sportsmanship and there should be no issues <:giga:1207390487284027502>

# Rotations
Notice how there are more players on the roster than there are spots (roster button, top of {clan_post_channel}) That means that there will be daily rotations where the worst or non-attacker will be rotated out with the person on the bench. Don't worry, we will aim to secure at least 8 stars for everyone such that you get the maximal CWL medals.
You can keep track of the rotations in the pinned messages in-game <:salute:1310280565034848257> 
# CWL Leadership <:chadHog:1217151645297672322> 
This isn't the wild west, so welcome our team
<@&1179731225464803368> {helper_cwl}
Quick summary:
<@&1179731225464803368>: Helping you to be on track with attacks, filling our defensive cc's and there to give attacking advice if you need it(, making roster rotations)
Turn to our helpers if you need assistance <:maybe:1209121676093165578> <:stonks:1209861452492709938> 

Let's have a nice and enjoyable CWL here <:stonks:1209861452492709938> <:ima:1209863041353654344>
""",
}
