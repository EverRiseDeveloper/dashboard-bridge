## A note on your data

- Nothing installs without someone in your household seeing the release notes, ticking a box and tapping Install.
- We keep a record on your own HA box of who approved each update, who installed it, who restarted Home Assistant for it, and when. This stays on your device — it isn't sent anywhere.
- Your home checks for new updates every 3 hours. Checking for and downloading updates only talks to our public GitHub repositories. No data about your home, your devices, or your household is sent in that request.
- When a new update is out, every phone and tablet with the Home Assistant app signed in to your home gets one notification. It travels the same way as your other Home Assistant notifications and only says which version is ready.
- We don't collect usage data, analytics, or any information about you through this feature.
- Any signed-in member of your household can approve and install an update, not only the account that originally set up the dashboard.
- Home Assistant only restarts for an update when the person installing it says yes. Nothing restarts on its own.
- The overnight summary is written on your own HA box from its logbook. Only if your household turns on "Write it with AI" in Settings → Home screen is the night's list (which lights, doors, locks and cameras, and when) sent to your home's AI, Google Gemini, to word it. It's off unless you turn it on.
- The energy plan's advice is written by your home's AI, Google Gemini, when Home Assistant has one set up. While someone has the dashboard open, at most every 10 minutes, it's sent your home's live energy numbers: what the sun, battery, house and grid are doing, the power prices for the coming hours, the car's charge and whether it's at home, the weather forecast, and your city (from Home Assistant's time zone). No names, addresses or account details are sent. The latest advice is kept on your own HA box.
- Home's light-hearted headline ("All good.", "All secure." and "Good night." in fresh words) is written by your home's AI, a Google Gemini Flash-Lite model when there is one, at most once an hour while someone has Home on screen. Nothing about your home is sent: no names, rooms, devices, states or place, only a request for three general lines. The latest lines are kept on your own HA box. It can be turned off in Settings → Home screen.
- Spotify, on the dashboard's Media page: when someone opens the Spotify browser, the bridge asks Spotify for your playlists, songs or search results, and starts what was picked on your speaker. It uses your household's own Spotify login, which Home Assistant already holds. Nothing about your home is sent to Spotify, and nothing is stored.

## What's new

- New updates now tell you. Your home checks for a new EverRise update every 3 hours, and when one is out, every phone and tablet with the Home Assistant app signed in to your home gets a notification. Tap it to go straight to Software update. Each version is announced once, and the same message goes in the Message Centre.
- Updates only install when someone taps Install on Software update. Home Assistant's own "Dashboard frontend" update, which could install a dashboard build without the release notes or the tick box, has been removed.
- No more overnight restarts. Before, if nobody restarted after a system update, Home Assistant restarted itself at 3 am. Now it only restarts when someone says yes. If they choose not now, the update takes effect the next time Home Assistant restarts.
- One install at a time. If two people tap Install together, the second sees who is already installing instead of installing it twice, and Home Assistant can't be restarted from Software update until the install has finished.
- Fixed: after choosing not to restart straight away, Software update offered the same system update again.
