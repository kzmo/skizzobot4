Skizzobot 4.1
=============
An IRC chatbot that creates sentences based on word relations (called
triplets) in a MongoDB.

Note that the bot needs to hang out in the channel and read the
conversation for a while before it can do anything else than
repeat back the original sentences.

Requirements
============
- Python 3.x (Tested with Python 3.5.3)
- MongoDB installed
- pymongo package for Python (pip install pymongo)
- irc package for Python (pip install irc)

Files
=====
skizzobot4.py: The bot script
config.ini: The configuration file
LICENSE: License information
readme.txt: This file

Configuration
=============
Edit or copy the config.ini to another file.

Multiple channels are allowed by comma separation. Multiple messages for
events are allowed separated by '|' characters.

How to run
==========
python skizzobot4.py configfile
