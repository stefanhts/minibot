from __future__ import annotations

import configparser
import csv
import datetime
import io
import re
import shlex
from dataclasses import dataclass, field

import disnake
import disnake.abc
import peewee

# www.maptap.gg October 14
# 95 97 82 84 76
# Final score: 836
BADGE_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?maptap\.gg (\w+ \d{1,2})\s*"
    "\n"
    r"([^\n]*\d[^\n]*)"  # Round scores, possibly mixed with emoji
    "\n"
    r"\s*Final score: (\d+)"
)

# Whether a higher score ranks higher on the leaderboard
HIGHER_IS_BETTER = True

database = peewee.SqliteDatabase("maptapbot.db")


@dataclass
class ParsedResult:
    """The fields extracted from a share message."""

    puzzle: str
    score: int
    details: str = ""


def parse_result(content: str) -> ParsedResult | None:
    """Extract a result from a share message, if it is one."""

    if match := BADGE_PATTERN.match(content.strip()):
        month_day, rounds, score = match.groups()
        try:
            date = infer_year(datetime.datetime.strptime(f"{month_day} 2000", "%B %d %Y"), datetime.date.today())
        except ValueError:
            return None
        details = " ".join(re.findall(r"\d+", rounds))
        return ParsedResult(puzzle=date.isoformat(), score=int(score), details=details)
    return None


def infer_year(month_day: datetime.datetime, today: datetime.date) -> datetime.date:
    """Pick the year that puts a yearless date closest to today.

    The year of month_day is ignored; parse with a leap year so February 29 is valid.
    """

    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(month_day.replace(year=year).date())
        except ValueError:  # February 29 in a non-leap year
            pass
    return min(candidates, key=lambda date: abs(date - today))


def format_score(score: int) -> str:
    return str(score)


def format_puzzle(puzzle: str) -> str:
    return f"MapTap {puzzle}"


class Result(peewee.Model):
    """A puzzle result for a user."""

    user_id = peewee.BigIntegerField()
    guild_id = peewee.BigIntegerField()
    timestamp = peewee.TimestampField(default=datetime.datetime.now, utc=True)

    puzzle = peewee.CharField(max_length=32)
    score = peewee.IntegerField()
    details = peewee.CharField(max_length=255, default="")

    class Meta:
        database = database
        indexes = ((("user_id", "guild_id", "puzzle"), True),)


@dataclass
class LeaderboardEntry:
    """A user on the leaderboard."""

    position: int
    member: disnake.Member
    score: int
    details: str
    puzzle: str | None = None


@dataclass
class Leaderboard:
    """Structured leaderboard data."""

    title: str
    entries: list[LeaderboardEntry] = field(default_factory=list)

    def render(self) -> str:
        """Render the leaderboard in a message."""

        if not self.entries:
            return f"No leaderboard for {self.title}"

        rows = []
        for entry in self.entries:
            end = " :crown:" if entry.position == 1 else ""
            details = f" {entry.details}" if entry.details else ""
            puzzle = f" · {format_puzzle(entry.puzzle)}" if entry.puzzle is not None else ""
            rows.append(f"{entry.position}. {entry.member.display_name} ({format_score(entry.score)}){details}{puzzle}{end}")

        return "\n".join(rows)


class Bot(disnake.Client):
    """Custom implementation of a command handler."""

    def __init__(self, config: configparser.ConfigParser) -> None:
        """Propagate config sections to plugins."""

        intents = disnake.Intents(messages=True, message_content=True, guilds=True)
        super().__init__(intents=intents)

        self.token = config.get("discord", "token")

    def run(self, *args: object, **kwargs: object) -> None:
        """Pass token if it's been configured."""

        token = {"token": self.token} if self.token is not None else {}
        return super().run(*args, **kwargs, **token)

    async def get_leaderboard(self, guild: disnake.Guild, puzzle: str) -> Leaderboard:
        """Construct the leaderboard for a guild and puzzle."""

        results = Result.filter(
            guild_id=guild.id,
            puzzle=puzzle,
        ).order_by(
            Result.score.desc() if HIGHER_IS_BETTER else Result.score.asc(),
            Result.timestamp.asc(),
        )

        return await self.rank(guild, format_puzzle(puzzle), list(results))

    async def get_all_time_leaderboard(self, guild: disnake.Guild) -> Leaderboard:
        """Construct a leaderboard of each user's best ever result in a guild."""

        results = Result.filter(
            guild_id=guild.id,
        ).order_by(
            Result.score.desc() if HIGHER_IS_BETTER else Result.score.asc(),
            Result.timestamp.asc(),
        )

        best: dict[int, Result] = {}
        for result in results:
            best.setdefault(result.user_id, result)

        return await self.rank(guild, "all time", list(best.values()), show_puzzle=True)

    async def rank(
        self,
        guild: disnake.Guild,
        title: str,
        results: list[Result],
        show_puzzle: bool = False,
    ) -> Leaderboard:
        """Rank already-sorted results, sharing positions on ties."""

        await guild.get_or_fetch_members([result.user_id for result in results])

        leaderboard = Leaderboard(title)
        position = 1
        last_score = None
        for result in results:
            if (member := guild.get_member(result.user_id)) is None:
                continue  # No longer in the guild
            if last_score is not None and result.score != last_score:
                position += 1
            puzzle = result.puzzle if show_puzzle else None
            leaderboard.entries.append(LeaderboardEntry(position, member, result.score, result.details, puzzle))
            last_score = result.score

        return leaderboard

    def get_latest_puzzle(self, guild: disnake.Guild) -> str | None:
        """Get the most recently submitted puzzle in a guild."""

        result = Result.filter(guild_id=guild.id).order_by(Result.timestamp.desc()).first()
        return result.puzzle if result is not None else None

    async def on_result(self, message: disnake.Message, parsed: ParsedResult) -> None:
        """Save the result and print the leaderboard."""

        result, created = Result.get_or_create(
            user_id=message.author.id,
            guild_id=message.guild.id,
            puzzle=parsed.puzzle,
            defaults=dict(score=parsed.score, details=parsed.details),
        )

        if not created:
            if result.score != parsed.score or result.details != parsed.details:
                result.score = parsed.score
                result.details = parsed.details
                result.save()

        leaderboard = await self.get_leaderboard(message.guild, parsed.puzzle)
        color: disnake.Color | None = None
        for entry in leaderboard.entries:
            if entry.position == 1:
                if entry.member.id == message.author.id:
                    color = disnake.Color.gold()

        embed = disnake.Embed(
            title=f"{message.author.display_name} scored {format_score(parsed.score)} on {format_puzzle(parsed.puzzle)}",
            description=leaderboard.render(),
            color=color,
        )

        await message.channel.send(embed=embed)
        await message.delete()

    async def on_message(self, message: disnake.Message):
        """Listen for result messages."""

        if message.author.bot:
            return

        elif (parsed := parse_result(message.content)) is not None:
            await self.on_result(message, parsed)
            return

        elif message.content.startswith("%maptap "):
            parts = shlex.split(message.content[8:].strip())
            if len(parts) == 0:
                return

            elif parts[0] == "l" or parts[0] == "leaderboard":
                puzzle = parts[1] if len(parts) >= 2 else self.get_latest_puzzle(message.guild)
                if puzzle is None:
                    await message.reply("No results yet")
                    return

                leaderboard = await self.get_leaderboard(message.guild, puzzle)
                await message.channel.send(leaderboard.render())
                return

            elif parts[0] == "a" or parts[0] == "alltime":
                leaderboard = await self.get_all_time_leaderboard(message.guild)
                await message.channel.send(leaderboard.render())
                return

            elif parts[0] == "d" or parts[0] == "dump":
                display_names = {}
                fp = io.StringIO()
                writer = csv.writer(fp)
                writer.writerow(("display_name", "timestamp", "puzzle", "score", "details"))
                for result in Result.filter(Result.guild_id == message.guild.id):
                    if (display_name := display_names.get(result.user_id)) is None:
                        display_name = display_names[result.user_id] = (await self.fetch_user(result.user_id)).display_name
                    writer.writerow((display_name, result.timestamp, result.puzzle, result.score, result.details))
                bfp = io.BytesIO(fp.getvalue().encode())
                await message.reply(file=disnake.File(bfp, "results.csv"))
                return

            else:
                await message.reply("Commands are `l/leaderboard [puzzle]`, `a/alltime`, and `d/dump`")
                return


def main():
    config = configparser.ConfigParser()
    config.read("maptapbot.conf")

    database.connect()
    database.create_tables([Result], safe=True)

    bot = Bot(config)

    # Print an add link based on configuration
    client_id = config["discord"]["client_id"]
    print(
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={client_id}"
        "&permissions=2415930432"
        "&scope=bot"
    )

    bot.run()


if __name__ == "__main__":
    main()
