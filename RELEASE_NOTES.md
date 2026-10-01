## A note on your data

- Nothing installs without someone in your household seeing the release notes and ticking a box first.
- We keep a record on your own HA box of who approved each update and when. This stays on your device — it isn't sent anywhere.
- Checking for and downloading updates only talks to our public GitHub repositories. No data about your home, your devices, or your household is sent in that request.
- We don't collect usage data, analytics, or any information about you through this feature.
- Any signed-in member of your household can approve and install an update, not only the account that originally set up the dashboard.
- If an update needs a restart and nobody restarts right away, Home Assistant will restart itself overnight to finish.
- The overnight summary is written on your own HA box from its logbook. Only if your household turns on "Write it with AI" in Admin → Home is the night's list (which lights, doors, locks and cameras, and when) sent to your home's AI, Google Gemini, to word it. It's off unless you turn it on.
- The energy plan's advice is written by your home's AI, Google Gemini, when Home Assistant has one set up. While someone has the dashboard open, at most every 10 minutes, it's sent your home's live energy numbers: what the sun, battery, house and grid are doing, the power prices for the coming hours, the car's charge and whether it's at home, the weather forecast, and your city (from Home Assistant's time zone). No names, addresses or account details are sent. The latest advice is kept on your own HA box.

## What's new

- The energy plan's advice now comes from your home's AI (a Gemini Flash model when there is one). The bridge asks it at most every 10 minutes for the whole home and shares the answer with every phone and tablet. When the AI is busy, the last advice stays, with when it was written, and it asks again 10 minutes later. Needs the EverRise dashboard 3.1.0.
- Last night's summary now also goes into the Message Centre each morning, one message per night, so it can be read after it leaves Home at 9 am.
- Every morning at 6 am, the dashboard writes what happened in the house between 11 pm and 6 am: doors, locks, the alarm, cameras, people coming and going, and which lights came on and why. Home shows it until midday.
