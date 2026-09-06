
# TriggerHappy - Discussion Moderation Tool

## Features

TriggerHappy is a moderation tool for GitHub discussions.

Give select users the ability to close discussions, give strikes to users, and ban users from opening any other discussions outright (and also the ability to unban them).



### Multiple Permission Levels

> Higher permissions give access to all the abilities of lower permissons as well

- `strikers`: Can give users strikes
- `closers`: Can close/open Discussions
- `moderators`: Can Ban/Unban people

### Optional Features

- Strikes: You can choose to enable strikes.
    - Auto-Banning when a user has reached the number of allowed strikes. This number is configurable.
- Format Enforcemen: Can auto-close discussions that do not match the provided format regex.
    - Limit enforcement to certain discussion categories (use category names!)
    - Auto-Strike: Can choose to give users strikes for their non-format-complicant discussion.
- Moderate multiple repos / Split storage repo to avoid commit spam in main repo
    - Allows you to keep the same list of privilaged and offending users across multiple repos. This can also be used to separate which repo stores the list of these users to avoid spamming your main repo(s) with moderation commits.


### Commands

Commands:

- `/strike`: strikes the discussion opener
- `/strike-target [@]<user>`: Strike a custom target.
- `/unstrike [@]<user>`: Remove a strike from a user


- `/close <resolved|outdated|duplicate> [note]`: Close the discussion with a github-provided reason, as well as an optional user-provided reason.
- `/open`: Re-opens a closed discussion

- `/ban <reason>`: Bans the discussion opener with a reason.
- `/unban [@]<user>`: Unbans a user


The ban author is `__system__` if user is banned by strikes.




## Configuration


See [Example Consumer Repo Structure](sampleConsumerRepoStructure) for a setup with all features enabled.

- List of users with various permissions are to be written in `config.yml`.
- You can manually manage banned users / Strike information per user by editing `banned.yml` and `strikes.yml` respectively


## Install

### Single Repo

See [Example Workflow](sampleConsumerRepoStructure/.github/workflows/discussion_moderation.yml) for how to integrate TriggerHappy to your repo.

Follow the instructions there to enable optional features.

### Multiple Repos

You need to set up "Fine-grained PAT" for the consumer repo to be able to act on the storage repo. Use `CROSS_REPO_TOKEN` to store the storage repo token.

See sampleConsumerRepoStructure/.github/workflows/discussion_moderation.yml for setting up Consumer Repo(s)

The paths paths across all consumer repos MUST BE SYNCED!

# To-Do

- [] Multiple Messages Per Comment
- [] Per-Strike Reasons

