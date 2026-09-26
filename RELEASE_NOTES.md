## A note on your data

- Nothing installs without someone in your household seeing the release notes and ticking a box first.
- We keep a record on your own HA box of who approved each update and when. This stays on your device — it isn't sent anywhere.
- Checking for and downloading updates only talks to our public GitHub repositories. No data about your home, your devices, or your household is sent in that request.
- We don't collect usage data, analytics, or any information about you through this feature.
- Any signed-in member of your household can approve and install an update, not only the account that originally set up the dashboard.
- If an update needs a restart and nobody restarts right away, Home Assistant will restart itself overnight to finish.

## What's new

- Automations built in the dashboard can now turn switches on and off, including lights and fans that Home Assistant runs as switches. This is what the dashboard's new "Turn a switch on or off" action needs.
- After a dashboard update, phones and tablets now open the new version straight away. Before, the Home Assistant app could keep showing the previous version until its cache was cleared by hand.
- Home Assistant's own update notice for the dashboard no longer keeps offering a version you've already installed from the Updates screen.
