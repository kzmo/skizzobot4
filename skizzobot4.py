"""Skizzobot 4.1 - An IRC chatbot based on Markov chains

Skizzobot is an IRC chatbot that stores word triplets in a Mongo database and
creates sentences based on that data. Only talks when the nick name is
mentioned.

Command line options:
  configfile  Configuration file
  -h, --help  show this help message and exit

:Authors:
    Janne Valtanen <valtanen.jp@gmail.com>

See readme.txt for further instructions
See LICENSE for copyright information.
"""

import configparser
import argparse
import random
import time
import json

import pymongo

import irc
import irc.client
import irc.modes

# Lenient character decoding so Latin-1 doesn't cause crash
from jaraco.stream import buffer
irc.client.ServerConnection.buffer_class = buffer.LenientDecodingLineBuffer


def random_msg(msg):
    """Return a random substring from a list of strings

    Used to get a random string from the configuration file multiple string
    configuration values.

    Args:
        msg(list): A list of strings

    Returns:
        str: Random substring.
    """

    return random.choice(msg)


def nick_from_source(source):
    """Returns the nickname string from event.source.

    Args:
        source(str): The event.source string.

    Returns:
        str: The IRC nickname in the source.
    """

    return source[:source.find("!")]


def find_first_triplet(triplets):
    """Select a starter 'triplet' to seed the line to be generated.

    Args:
        triplets(list): The list of triplet dictionaries from MongoDB.

    Returns:
        triplet(dict): The triplet dictionary used for line seeding.
    """

    # Count the total sum of how many times the triplets have appeared in
    # the past.
    tsum = 0
    for triplet in triplets:
        tsum += triplet["count"]

    # Pick a random triplet from the list weighted by the cont it has appeared.
    randsum = random.randint(1, tsum)
    csum = 0
    for triplet in triplets:
        csum += triplet["count"]
        if csum >= randsum:
            break

    # Return the selected triplet.
    return triplet


class Skizzobot:
    """The bot class.

    Just call __init__ without parameters and then run.

    Attributes:
        cfg(dict): The configuration dictionary.
        revengelist(set): The users to be had revenged upon.
        database(object): The MongoDB connection object.
        last_join(float): The last time join happened in seconds.
    """

    # Configuration dictionary where the information from the configuration
    # file is loaded into.
    cfg = {}

    # A set of nicks that have angered the bot in the past.
    revengelist = set()

    # The MongoDB connection
    database = None

    # How many seconds ago was the last join information. This is used to
    # not flood the channel with welcome messages in the case of netsplit.
    last_join = 0.0

    def __init__(self):
        print("Starting Skizzobot 4.1..")

        # Load configuration from the configuration file specified in the
        # commandline.
        self.load_configuration()

        # Set up the database connection.
        self.database = self.setup_db()

    def run(self):
        """Run the bot.
        """

        # Run the bot forever until exception kills it.
        while True:
            try:
                # Open IRC connection.
                reactor, conn = self.setup_irc_connection()
                # Run the IRC reactor for ever. All events will be catched
                # by the callbacks.
                reactor.process_forever()
            except irc.client.IRCError as ex:
                # In case the IRC connection failed or broke down wait for
                # one second and try again.
                print("Crashed. Reason: "+str(ex))
                time.sleep(1)
            try:
                # Try to disconnect if it's possible before the reconnect.
                conn.close()
            except irc.client.IRCError as ex:
                print("Disconnect failed. Reason: " + str(ex))
            print("Restarting..")

    # Callback functions for the IRC library
    def on_connect(self, conn):
        """Callback when the IRC connection to server is made.

        Just joins all the configured channels.

        Args:
            conn(object): The connection object.
        """

        # Join all channels listed in the configuration.
        for channel in self.cfg['channels']:
            if irc.client.is_channel(channel):
                print("Joining channel: "+channel)
                conn.join(channel)

    def on_join(self, conn, event):
        """Callback when a channel is joined by bot or any other user.

        Sends a hello message based on if the bot or joining or someone else.

        Args:
            conn(object): The connection object.
            event(object): The event object.
        """

        # Get the nickname for who is joining.
        nick = nick_from_source(event.source)

        # If the bot itself joined then say hello to the channel.
        if nick == self.cfg['nick']:
            conn.privmsg(event.target,
                         random_msg(self.cfg['hello_channel']))
        else:
            # In case it was another user then say hello to user.

            # The last_join timer is to prevent the bot to say hello to
            # all users returning from the split.

            # This needs a better solution, but the IRC
            # library does not have a split detection.
            if time.time() - self.last_join > 1.0:
                conn.privmsg(event.target, nick + ": " +
                             random_msg(self.cfg['hello_user']))
            self.last_join = time.time()

    def on_kick(self, conn, event):
        """Callback when someone is kicked from any of the channels.

        If the bot itself is kicked the user doing the kicking is added to
        the revengelist.

        Args:
            conn(object): The connection object.
            event(object): The event object.
        """

        # Get the nick of who got kicked.
        kicked = event.arguments[0]

        # If the bot itself got kicked then rejoin the channel and add the
        # kicker to the revenge list.
        if kicked == self.cfg['nick']:
            print("Got kicked.. rejoining")
            conn.join(event.target)
            nick = nick_from_source(event.source)
            conn.privmsg(event.target, nick + ": " +
                         random_msg(self.cfg['revenge']))
            self.revengelist.add(nick)

    def on_pubmsg(self, conn, event):
        """Callback when a public message appears on any channel.

        Store the message data to database.
        If bot nick name appears in message then respond to channel.

        Args:
            conn(object): The connection object.
            event(object): The event object.
        """

        # Get bot nick.
        nick = self.cfg['nick']

        # Get the actual channel message.
        msg = event.arguments[0]

        # Split the message based on spaces.
        splitted = msg.split(" ")

        # Remove empty strings from the list.
        splitted = list(filter(lambda x: x != "", splitted))

        # Handle channel message and get the possible seed word
        # for response.
        starter = self.handle_channel_msg(splitted, event.target)

        # If the bot nick was included in the message then respond.
        if msg.find(nick) != -1:

            # 1 out of 5 times a random word from the original
            # message is used as a seed.
            if random.randint(0, 5) != 0:
                starter = ""

            # Generate the response message.
            msg = self.create_sentence(event.target,
                                       starter)

            # There might words with linefeeds in the database
            # due to a bug in the older version.
            msg = msg.replace('\n', ' ').replace('\r', '')

            # Send response to IRC.
            conn.privmsg(event.target, msg)

    def on_mode(self, conn, event):
        """Callback when channel mode changes.

        If bot gets operator status go through the revenge list.
        If bot loses operator status respond with a message.

        Args:
            conn(object): The connection object.
            event(object): The event object.
        """

        # Parse the channel modes got in the mode string.
        modes = irc.modes.parse_channel_modes(" ".join(event.arguments))

        # Get the nick for the client who did the mode change(s).
        nick = nick_from_source(event.source)

        status_change = None

        # Iterate through all the mode changes because there can be
        # multiple at the same time.
        for mode in modes:

            # If bot itself was the target of the mode change then
            # process.
            if mode[2] == self.cfg['nick']:
                # Check if the mode change was adding or removing
                # the operator status.
                # NOTE: Both adding and removing can happen multiple
                # times in the same mode change command!
                if mode[1] == "o":
                    if mode[0] == '+':
                        status_change = "+o"
                    elif mode[0] == "-":
                        status_change = "-o"

            # Respond based on the *last* status change in the
            # mode change command.
            if status_change == "+o":
                conn.privmsg(event.target, nick + ": " +
                             random_msg(self.cfg['thanks']))
                self.apply_revengelist(conn, event.target)
            elif status_change == "-o":
                conn.privmsg(event.target, nick + ": " +
                             random_msg(self.cfg['disappointment']))

    def handle_channel_msg(self, splitted, channel):
        """ Handle a single message to a channel

        Args:
            splitted(list): The message to a channel splitted into a list
                            of words.
            channel(str): The channel where the message was received.

        Returns:
            str: A random string from the splitted list that can be used
                 as a starter word for a line.
        """

        # Get my own nick name.
        nick = self.cfg['nick']

        # Remove the nick starter word.
        # We don't want to store the first nick addressing to database.
        if splitted and splitted[0].startswith(nick):
            splitted = splitted[1:]

        # Empty lines don't need to be handled.
        if not splitted:
            return

        # Construct the triplets data.
        triplets = []
        triplet = [''] + splitted + ['']
        for i in range(len(triplet) - 2):
            triplets.append([triplet[i], triplet[i+1], triplet[i+2]])

        # Store the triplets to the database.
        self.store_triplets(channel, triplets)

        # Return random word from the original line, excluding the
        # possible starting bot nickname.
        return random.choice(splitted)

    def store_triplets(self, channel, triplets):
        """Store the triplets from the public channel message.

        Add the triplets information to the database.
        If the triplet has appeared before then add to count. Otherwise
        create a new item.

        Args:
            channel(str): The channel name where the triplets appeared.
            triplets(list): The triplets list.

        """

        # Store the triplets.
        for triplet in triplets:
            left = triplet[0]
            middle = triplet[1]
            right = triplet[2]

            # Check if we have one already?
            old = self.database.triplets.find_one({"channel": channel,
                                                   "left": left,
                                                   "middle": middle,
                                                   "right": right})

            # If we don't have this triplet then store it as a new entry
            # with a count of one.
            if old is None:
                self.database.triplets.insert({"channel": channel,
                                               "left": left,
                                               "middle": middle,
                                               "right": right,
                                               "count": 1})
            # If we already have this entry then add to the count.
            else:
                count = old["count"]
                count += 1
                self.database.triplets.update(old,
                                              {"$set": {"count": count}},
                                              upsert=True)

    def get_triplets(self, channel, starter):
        """Get the triplets based on starter.

        If the starter is an empty string find triplets that have the left
        string as an empty string. In other triplets that finish the left
        side of the string.
        If the starter is a non-empty string find triplets where the starter
        is the middle string in the triplet.

        Args:
            channel(str): The channel name in a string.
            starter(str): The starter word for the triplet that is used as
                          a seed.

        Returns:
            list: List of triplet dictionaries from the database.
        """

        # If starter seed is an empty string we start constructing the line
        # from the left by having the left word in the triplet as an empty
        # string.
        if starter == "":
            return list(self.database.triplets.find({"channel": channel,
                                                     "left": ""}))
        else:
            # If the seed is a non-empty string we use it as the middle
            # word.
            return list(self.database.triplets.find({"channel": channel,
                                                     "middle": starter}))

    def create_sentence(self, channel, starter=""):
        """Create a sentence based on channel name and starter seed word.

        If starter is empty then no seed is used and the sentence is
        generated from left to right.

        Args:
            channel(str): The channel name.
            starter(str): The starter seed. Empty string by default.

        Returns:
            str: The generated sentence string.
        """

        # Get triplets from the database based on the starter word
        # and the channel name.
        triplets = self.get_triplets(channel, starter)

        # Find the first triplet to seed the line.
        triplet = find_first_triplet(triplets)

        # We know the three first words now..
        left = triplet["left"]
        middle = triplet["middle"]
        right = triplet["right"]

        starttriplet = triplet

        # First part of the sentence based on the string.
        # Not necessarily the start because the line might
        # constructed starting from the middle.
        sentence = left+" "+middle+" "+right

        # Create the end of the sentence..
        while right != "":
            # Find the triplets that continue the sentence to the right.
            # Current middle word is the new left and the current right
            # is the new middle.
            triplets = list(self.database.triplets.find({"channel": channel,
                                                         "left": middle,
                                                         "middle": right}))

            # Count the total sum of triplets based on triplet entry
            # counts.
            tsum = 0
            for triplet in triplets:
                tsum += triplet["count"]
            # Pick a random triplet weighted by the counts.
            if tsum > 1:
                randsum = random.randint(1, tsum)
            else:
                randsum = 1
            csum = 0

            # Go through the list until we find the corresponding triplet.
            for triplet in triplets:
                csum += triplet["count"]
                if csum >= randsum:
                    break

            # Switch the variables to correspond the new triplet.
            middle = triplet["middle"]
            right = triplet["right"]

            # Add to sentence string.
            sentence += " "+right

        # Construct the starting of the sentence in case we started
        # from the middle (or the end).
        left = starttriplet["left"]
        middle = starttriplet["middle"]
        right = starttriplet["right"]

        # We didn't start from the beginning if the left word was not an
        # empty string.
        while left != "":
            # Find the triplets that continue the sentence to the left.
            # Current left word is the new middle and the current middle
            # is the new right.

            triplets = list(self.database.triplets.find({"channel": channel,
                                                         "middle": left,
                                                         "right": middle}))

            # Count the total sum of triplets based on triplet entry
            # counts.
            tsum = 0
            for triplet in triplets:
                tsum += triplet["count"]
            # Pick a random triplet weighted by counts.
            randsum = random.randint(0, tsum)

            # Go through the list until we find the corresponding triplet.
            csum = 0
            for triplet in triplets:
                csum += triplet["count"]
                if csum >= randsum:
                    break

            # Switch the variables to correspond the new triplet.
            middle = triplet["middle"]
            left = triplet["left"]

            # Add to sentence string.
            sentence = left+" "+sentence

        # Remove unnecessary space from start
        # (buggy data due to earlier versions).
        if sentence[0] == " ":
            sentence = sentence[1:]

        # Return the whole constructed sentence.
        return sentence

    def load_configuration(self):
        """Load configuration file and store to configuration dictionary.

        The configuration file name is got from the commandline.
        """

        # Parse the command line arguments and get the configuration file
        # name.
        parser = argparse.ArgumentParser(description='Skizzobot! 4.0')
        parser.add_argument('configfile',
                            type=str,
                            help="Configuration file")
        args = parser.parse_args()

        print("Loading configuration file: "+args.configfile)

        # Load and process the configuration file.
        config = configparser.ConfigParser()
        config.read(args.configfile)

        # Unpack the configuration into self.cfg
        self.cfg['nick'] = config['User']['nick']

        self.cfg['hostname'] = config['Server']['hostname']
        self.cfg['port'] = int(config['Server']['port'])

        channelstr = config['Channels']['active']
        self.cfg['channels'] = channelstr.split(',')

        self.cfg['dbname'] = config['Database']['name']

        self.cfg['hello_channel'] = json.loads(config['Messages']['hello_channel'])
        self.cfg['hello_user'] = json.loads(config['Messages']['hello_user'])
        self.cfg['revenge'] = json.loads(config['Messages']['revenge'])
        self.cfg['thanks'] = json.loads(config['Messages']['thanks'])
        self.cfg['kick_message'] = json.loads(config['Messages']['kick'])
        self.cfg['disappointment'] = json.loads(config['Messages']['disappointment'])

    def setup_db(self):
        """Setup the MongoDB connection.

        The connection parameters are got from the configuration file.

        Returns:
            obj: Database connection object.
        """

        # Set up the database client connection.
        client = pymongo.MongoClient()
        dbname = self.cfg['dbname']
        print("Connecting to MongoDB: "+dbname)
        database = client.get_database(dbname)
        print("Connected!")

        # Return the database object.
        return database

    def setup_irc_connection(self):
        """Setup the IRC connection.

        The connection parameters are got from the configuration file.

        Returns:
            obj: The IRC reactor object.
            obj: The IRC connection object.
        """

        # Get the IRC client reactor.
        reactor = irc.client.Reactor()
        print("Connecting to IRC server " + self.cfg['hostname'] +
              " at port " + str(self.cfg['port']))

        # Open connection.
        irc_conn = reactor.server().connect(self.cfg['hostname'],
                                            self.cfg['port'],
                                            self.cfg['nick'])
        print("Connected!")

        # Register all callbacks.

        # Server join callback.
        irc_conn.add_global_handler("welcome",
                                    lambda conn, event:
                                    Skizzobot.on_connect(self, conn))
        # Channel join callback (all joins).
        irc_conn.add_global_handler("join",
                                    lambda conn, event:
                                    Skizzobot.on_join(self, conn, event))
        # Kick event callback.
        irc_conn.add_global_handler("kick",
                                    lambda conn, event:
                                    Skizzobot.on_kick(self, conn, event))
        # Private messsage callback not implemented yet.
        irc_conn.add_global_handler("privmsg",
                                    lambda conn, event:
                                    print("Got privmsg event: " +
                                          str(event)))
        # Public message on channel callback.
        irc_conn.add_global_handler("pubmsg",
                                    lambda conn, event:
                                    Skizzobot.on_pubmsg(self, conn, event))
        # Mode change event callback.
        irc_conn.add_global_handler("mode",
                                    lambda conn, event:
                                    Skizzobot.on_mode(self, conn, event))

        # Return the reactor and connection objects.
        return reactor, irc_conn

    def apply_revengelist(self, conn, target):
        """Apply the revengelist and kick everybody on it.

        Args:
            conn(object): The IRC connection object
            target(str): The IRC target (the channel name).
        """

        # If revenge list had items kick all users in the list.
        if self.revengelist:
            for nick in self.revengelist:
                conn.kick(target,
                          nick,
                          random_msg(self.cfg['kick_message']))
            # Empty the list (no multiple revenges).
            self.revengelist = set()


def main():
    """Main function. Just run the bot.
    """

    # Init the bot.
    bot = Skizzobot()
    # Run the bot.
    bot.run()


if __name__ == "__main__":
    main()
