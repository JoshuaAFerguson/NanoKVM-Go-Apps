# Pixel Pet

A tiny Tamagotchi-style blob that lives on your NanoKVM's screen.

| Button | Does |
| --- | --- |
| 🍎 Feed | Fills its belly (it refuses when stuffed) |
| 🟠 Play | Cheers it up, costs some energy. Becomes ➕ **Heal** when it's sick |
| 🫧 Wash | Cleans up droppings and gives it a bath |
| 🌙 Sleep / ☀️ Wake | Lights out. It recovers energy while asleep |

Tap the pet to give it a pat. The four bars show its belly, happiness, energy and
cleanliness.

## Growing up

**Egg** (hatches about 2 minutes after you first open it), then **baby**, then
**kid** (from day 1), then **adult** (from day 3). The adult form depends on how well
you looked after it:

- ✨ **Sparkle**: lovingly cared for
- 😎 **Chill**: doing fine
- 😠 **Grumpy**: neglected (it'll still forgive you)

It **never dies**. Needs hit zero and it gets sad or sick, but good care always brings it
back. Time keeps passing while the App is closed, at a quarter speed and for at most
12 pet-hours, so a weekend away is fine.

## It notices the KVM

- **Company:** it's happier while the KVM's target is showing video.
- **Bedtime:** between 22:00 and 07:00 it tires faster and dozes off when it runs low.
- **Fever:** if the KVM runs hot (SoC ≥ 70 °C) it feels sick until things cool down
  and you heal it.

Each device raises its own pet. The pet is saved in `/var/lib/nanokvm-apps/pixel-pet/`,
so it survives App updates. Set **Pet name** in the App's settings.
