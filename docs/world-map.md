# The world: places, actions and what each is for

Every action must do something: ease a need (the `inner:` effects), change the world, or open something later. This table is
the contract; `tests/test_individuals.py::test_every_new_action_does_something_for_a_need_or_changes_the_world` enforces it.

```
            garden ── workshop ── lane ── fortune_booth
              │                    │
 lookout ── fountain ── market ────┘
              ├── art_room
              └── terminal
```

| Place | Action | Purpose |
|---|---|---|
| garden | sit among the plants | security, solitude, stress |
| | water the plants | accomplishment; herbs and flowers grow (a consequence) |
| | pick herbs and eat them | a second, slower food source |
| | plant a seed | uses a seed from the pocket; fuller flowers later |
| lookout | look out over the square | novelty; the view depends on the weather |
| | watch the sunrise (dawn only) | novelty and security at a fixed time of day |
| | wind the clock (hidden, every 12 h) | accomplishment as a recurring duty |
| | stand in the wind (windy only) | stress and frustration relief |
| workshop | whittle something | expression and accomplishment; makes a keepsake that stays on the shelf |
| | sweep the floor | accomplishment; earns a coin |
| | go through the drawers | novelty; may find seeds |
| | look over what you have made | satisfaction and confidence from its own work |
| lane | sit on the bench | solitude, security |
| | watch people go by (daytime) | company without obligation: social, belonging |
| | watch the cat (when it is there) | belonging; trust builds slowly |
| | stroke the cat (after trust) | belonging, security, loneliness |
| fortune booth | have your fortune told (a coin) | random fortunes that ease or unsettle it; remembered; opens a conversation |
| | ask what it means (after a fortune) | social contact, belonging |
| | give her something you made | belonging, letting go of its own work |
| market | read a new letter | social, belonging; one decision at a time |
| | write back and pin the letter | keeps the knowledge: the original is pinned (board of 8) |
| | set the letter aside for good | forgets the letter entirely |
| | reread a pinned letter | the original as written, with its reply |
| | take a letter down for good | frees a place on the board; forgets the letter |

Chains: sweep (workshop) → coin → fortune (booth); whittle → keepsake → give to the fortune teller or look it over; drawers →
seed → plant (garden) → water → flowers and herbs → eat.
