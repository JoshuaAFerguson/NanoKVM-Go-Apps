"""Tests for pixel-pet/petsim.py (pure pet simulation)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pixel-pet"))

import petsim  # noqa: E402
from petsim import Env, Pet  # noqa: E402

HOUR = 3600
CALM = Env(video=False, night=False, hot=False)


def grown(stage_hours: float) -> Pet:
    pet = Pet.new(name="Blip", now=0)
    pet = petsim.hatch(pet, now=0)
    return petsim.advance(pet, now=stage_hours * HOUR, env=CALM, open_app=True, tend=True)


class LifecycleTest(unittest.TestCase):
    def test_egg_hatches_after_a_while_open(self):
        pet = Pet.new(name="Blip", now=0)
        self.assertEqual(pet.stage, "egg")
        self.assertEqual(petsim.advance(pet, now=60, env=CALM, open_app=True).stage, "egg")
        self.assertEqual(petsim.advance(pet, now=petsim.HATCH_S + 1, env=CALM, open_app=True).stage, "baby")

    def test_stages_by_age(self):
        self.assertEqual(grown(12).stage, "baby")
        self.assertEqual(grown(30).stage, "kid")
        self.assertEqual(grown(80).stage, "adult")

    def test_adult_form_depends_on_care(self):
        self.assertEqual(grown(80).form, "sparkle")   # tended throughout
        neglected = petsim.hatch(Pet.new(name="Blip", now=0), now=0)
        neglected = petsim.advance(neglected, now=80 * HOUR, env=CALM, open_app=True, tend=False)
        self.assertEqual(neglected.form, "grumpy")


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.pet = petsim.hatch(Pet.new(name="Blip", now=0), now=0)

    def test_needs_decay_but_never_below_zero_and_never_dies(self):
        later = petsim.advance(self.pet, now=1000 * HOUR, env=CALM, open_app=True)
        for value in (later.fullness, later.happiness, later.hygiene, later.energy):
            self.assertGreaterEqual(value, 0)
        self.assertNotEqual(later.stage, "dead")

    def test_closed_time_is_slowed_and_capped(self):
        open_ = petsim.advance(self.pet, now=4 * HOUR, env=CALM, open_app=True)
        closed = petsim.advance(self.pet, now=4 * HOUR, env=CALM, open_app=False)
        self.assertGreater(closed.fullness, open_.fullness)
        week = petsim.advance(self.pet, now=7 * 24 * HOUR, env=CALM, open_app=False)
        cap = petsim.advance(self.pet, now=petsim.MAX_CATCHUP_S / petsim.CLOSED_RATE, env=CALM, open_app=False)
        self.assertAlmostEqual(week.fullness, cap.fullness, delta=1)

    def test_feed_play_clean_sleep(self):
        hungry = petsim.advance(self.pet, now=6 * HOUR, env=CALM, open_app=True)
        fed = petsim.act(hungry, "feed", now=6 * HOUR)
        self.assertGreater(fed.fullness, hungry.fullness)
        played = petsim.act(hungry, "play", now=6 * HOUR)
        self.assertGreater(played.happiness, hungry.happiness)
        self.assertLess(played.energy, hungry.energy)
        dirty = Pet(**{**hungry.__dict__, "poops": 2, "hygiene": 10})
        clean = petsim.act(dirty, "clean", now=6 * HOUR)
        self.assertEqual((clean.poops, clean.hygiene), (0, 100))
        asleep = petsim.act(hungry, "sleep", now=6 * HOUR)
        self.assertTrue(asleep.asleep)
        rested = petsim.advance(asleep, now=8 * HOUR, env=CALM, open_app=True)
        self.assertGreater(rested.energy, asleep.energy)

    def test_overfeeding_has_diminishing_returns(self):
        pet = self.pet
        for _ in range(10):
            pet = petsim.act(pet, "feed", now=0)
        self.assertLessEqual(pet.fullness, 100)
        self.assertLess(pet.happiness, 100)  # stuffed pets aren't happier

    def test_immutability(self):
        before = dict(self.pet.__dict__)
        petsim.act(self.pet, "feed", now=10)
        petsim.advance(self.pet, now=HOUR, env=CALM, open_app=True)
        self.assertEqual(self.pet.__dict__, before)


class KvmTieInTest(unittest.TestCase):
    def setUp(self):
        self.pet = petsim.hatch(Pet.new(name="Blip", now=0), now=0)

    def test_target_video_cheers_it_up(self):
        alone = petsim.advance(self.pet, now=3 * HOUR, env=CALM, open_app=True)
        company = petsim.advance(self.pet, now=3 * HOUR, env=Env(video=True, night=False, hot=False), open_app=True)
        self.assertGreater(company.happiness, alone.happiness)

    def test_hot_device_makes_it_sick_and_heal_works_once_cool(self):
        sick = petsim.advance(self.pet, now=HOUR, env=Env(video=False, night=False, hot=True), open_app=True)
        self.assertTrue(sick.sick)
        self.assertTrue(petsim.act(sick, "heal", now=HOUR).sick is False)

    def test_night_makes_it_sleepy(self):
        day = petsim.advance(self.pet, now=2 * HOUR, env=CALM, open_app=True)
        night = petsim.advance(self.pet, now=2 * HOUR, env=Env(video=False, night=True, hot=False), open_app=True)
        self.assertLess(night.energy, day.energy)


class MoodTest(unittest.TestCase):
    def test_mood_priority(self):
        base = petsim.hatch(Pet.new(name="Blip", now=0), now=0)
        self.assertEqual(petsim.mood(base), "happy")
        self.assertEqual(petsim.mood(Pet(**{**base.__dict__, "sick": True})), "sick")
        self.assertEqual(petsim.mood(Pet(**{**base.__dict__, "fullness": 10})), "hungry")
        self.assertEqual(petsim.mood(Pet(**{**base.__dict__, "asleep": True})), "asleep")


class ClockTest(unittest.TestCase):
    """The KVM has no RTC: the wall clock can jump years forward (NTP) or backwards."""

    def setUp(self):
        self.pet = petsim.hatch(Pet.new(name="Blip", now=1_000_000), now=1_000_000)

    def test_huge_forward_jump_while_open_is_capped_and_fast(self):
        import time
        start = time.monotonic()
        later = petsim.advance(self.pet, now=1_000_000 + 50 * 365 * 24 * HOUR, env=CALM, open_app=True)
        self.assertLess(time.monotonic() - start, 2.0)
        self.assertLessEqual(later.age_s, petsim.MAX_OPEN_S + 1)

    def test_backwards_jump_resyncs_instead_of_freezing(self):
        back = petsim.advance(self.pet, now=10, env=CALM, open_app=True)
        self.assertEqual(back.last_update, 10)
        self.assertEqual(back.age_s, self.pet.age_s)
        later = petsim.advance(back, now=10 + HOUR, env=CALM, open_app=True)
        self.assertGreater(later.age_s, back.age_s)

    def test_non_finite_last_update_recovers(self):
        broken = Pet(**{**self.pet.__dict__, "last_update": float("inf")})
        fixed = petsim.advance(broken, now=5000, env=CALM, open_app=True)
        self.assertEqual(fixed.last_update, 5000)


class PersistenceTest(unittest.TestCase):
    def test_sanitizes_corrupt_values(self):
        pet = Pet.from_dict({"name": "X", "stage": "adult", "form": "weird", "fullness": 1e9, "happiness": float("nan"),
                             "poops": 10 ** 9, "care_n": -5, "care_sum": float("nan"), "age_s": float("inf"),
                             "egg_s": -3.0}, now=0)
        self.assertEqual((pet.fullness, pet.poops, pet.care_n), (100.0, petsim.MAX_POOPS, 0))
        self.assertTrue(0 <= pet.happiness <= 100)
        self.assertIn(pet.form, ("", "sparkle", "chill", "grumpy"))
        import math
        self.assertTrue(all(math.isfinite(getattr(pet, f)) for f in ("age_s", "egg_s", "care_sum", "last_update")))

    def test_overflowing_number_starts_fresh_egg(self):
        self.assertEqual(Pet.from_dict({"name": "X", "fullness": 10 ** 400}, now=0).stage, "egg")

    def test_care_form_never_crashes(self):
        state = {"care_sum": float("nan"), "care_n": 3}
        self.assertIn(petsim._care_form(state), ("sparkle", "chill", "grumpy"))

    def test_round_trip_and_tolerates_junk(self):
        pet = grown(30)
        self.assertEqual(Pet.from_dict(pet.to_dict()), pet)
        self.assertEqual(Pet.from_dict({"name": 5, "fullness": "x"}, now=0).stage, "egg")



class SpriteTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared"))
        import sprites
        self.sprites = sprites

    def test_all_sprites_are_rectangular_and_use_known_colours(self):
        s = self.sprites
        for name, rows in (("egg", s.EGG), ("baby", s.BABY), ("kid", s.KID), ("adult", s.ADULT_BODY),
                           *[(f"topper-{k}", v) for k, v in s.TOPPERS.items()], *s.ICONS.items()):
            widths = {len(r) for r in rows}
            self.assertEqual(len(widths), 1, name)
        for stage, form in (("egg", ""), ("baby", ""), ("kid", ""), ("adult", "sparkle"), ("adult", "chill"),
                            ("adult", "grumpy")):
            rows, palette, _ = s.body(stage, form)
            used = {ch for row in rows for ch in row} - {"."}
            self.assertLessEqual(used, set(palette), (stage, form))
        for name, rows in s.ICONS.items():
            used = {ch for row in rows for ch in row} - {"."}
            self.assertLessEqual(used, set(s.ICON_PALETTE), name)

    def test_face_anchors_inside_bodies(self):
        s = self.sprites
        for key, (eye_r, left, right, mouth_r, mouth_c) in s.FACES.items():
            rows = {"baby": s.BABY, "kid": s.KID, "adult": s.ADULT_BODY}[key]
            for r, c in ((eye_r, left), (eye_r, right + 1), (mouth_r, mouth_c)):
                self.assertNotEqual(rows[r][c], ".", (key, r, c))



import random  # noqa: E402

import motion  # noqa: E402


class MotionTest(unittest.TestCase):
    def setUp(self):
        self.m = motion.Motion(half_range=80, rng=random.Random(1))

    def run_for(self, seconds, **state):
        poses = []
        t = 0.0
        while t < seconds:
            poses.append(self.m.update(0.1, t, **state))
            t += 0.1
        return poses

    def test_wanders_within_bounds_and_turns(self):
        poses = self.run_for(60, can_walk=True, asleep=False)
        xs = [p.x for p in poses]
        self.assertLessEqual(max(abs(x) for x in xs), 80)
        self.assertGreater(max(xs) - min(xs), 40)  # actually moves around
        self.assertEqual({p.facing for p in poses}, {-1, 1})

    def test_stays_put_when_it_cannot_walk(self):
        poses = self.run_for(20, can_walk=False, asleep=True)
        self.assertEqual({p.x for p in poses}, {poses[0].x})

    def test_squash_on_landing_and_stretch_in_the_air(self):
        poses = self.run_for(60, can_walk=True, asleep=False)
        airborne = [p for p in poses if p.lift > 2]
        grounded_squash = [p for p in poses if p.lift == 0 and p.sy < 0.95]
        self.assertTrue(airborne)
        self.assertTrue(all(p.sy > 1.0 and p.sx < 1.0 for p in airborne))  # stretch while hopping
        self.assertTrue(grounded_squash)                                     # squash on landing
        self.assertTrue(all(p.sx > 1.0 for p in grounded_squash))            # volume preserved-ish

    def test_breathing_when_idle(self):
        poses = self.run_for(6, can_walk=False, asleep=False)
        self.assertGreater(max(p.sy for p in poses) - min(p.sy for p in poses), 0.02)

    def test_jump_reaction(self):
        self.m.jump(now=0.0)
        poses = self.run_for(1.0, can_walk=False, asleep=False)
        self.assertGreater(max(p.lift for p in poses), 8)


class RefusalTest(unittest.TestCase):
    def test_stuffed_pet_refuses_food(self):
        import petsim
        pet = petsim.Pet(name="P", stage="baby", fullness=97.0)
        self.assertTrue(petsim.refuses(pet, "feed"))
        self.assertFalse(petsim.refuses(petsim.Pet(name="P", stage="baby", fullness=50.0), "feed"))
        self.assertTrue(petsim.refuses(petsim.Pet(name="P", stage="baby", asleep=True), "play"))
        self.assertFalse(petsim.refuses(pet, "clean"))


if __name__ == "__main__":
    unittest.main()
