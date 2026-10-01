## A note on your data

- Nothing installs without someone in your household seeing the release notes and ticking a box first.
- We keep a record on your own HA box of who approved each update and when. This stays on your device — it isn't sent anywhere.
- Checking for and downloading updates only talks to our public GitHub repositories. No data about your home, your devices, or your household is sent in that request.
- We don't collect usage data, analytics, or any information about you through this feature.
- Any signed-in member of your household can approve and install an update, not only the account that originally set up the dashboard.
- If an update needs a restart and nobody restarts right away, Home Assistant will restart itself overnight to finish.
- The overnight summary is written on your own HA box from its logbook. Only if your household turns on "Write it with AI" in Admin → Home is the night's list (which lights, doors, locks and cameras, and when) sent to your home's AI, Google Gemini, to word it. It's off unless you turn it on.

## What's new

- Last night's summary now also goes into the Message Centre each morning, one message per night, so it can be read after it leaves Home at 9 am.
- Every morning at 6 am, the dashboard writes what happened in the house between 11 pm and 6 am: doors, locks, the alarm, cameras, people coming and going, and which lights came on and why. Home shows it until midday.
- Admin → Rooms can now list what your Broadlink remote has learnt, so a fan's speed buttons are set up by picking its name from the list instead of typing it. Only the names are read, only for an admin, and nothing leaves your Home Assistant.
