import unittest

from app.chat_dialogue import NEED_CELL, resolve


class DialogueTests(unittest.TestCase):
    def test_social_and_mixed(self):
        for greeting in ("ку", "куку!", "Салам алейкум", "Вечір в хату"):
            self.assertTrue(resolve(greeting, []).answer)
        self.assertEqual(resolve("Привіт, які тут парки?", []).question, "які тут парки?")

    def test_rank_edits_survive_social_turn(self):
        first = resolve("Покажи 5 найкращих гексів", [])
        history = [{"activeQuestion": first.question, "activeIntent": first.intent}, {"social": True}]
        worst = resolve("і найгірших", history)
        self.assertEqual((worst.intent, worst.question), ("top_hexagons", "Покажи 5 найгірших гексів"))
        history.append({"activeQuestion": worst.question, "activeIntent": worst.intent})
        self.assertEqual(resolve("а тепер 10", history).question, "Покажи 10 найгірших гексів")

    def test_confirm_only_whole_offer(self):
        turn = {"nextPrompt": "Які тут парки?", "offerIntent": "poi", "offerQuestion": "Які тут парки?"}
        self.assertEqual(resolve("Так, зроби!", [turn]).intent, "poi")
        self.assertFalse(resolve("так, зроби і дай рецепт", [turn]).intent)
        self.assertTrue(resolve("Так, зроби", []).answer)

    def test_add_replace_and_missing_cell(self):
        history = [{"activeQuestion": "Які тут парки?", "activeIntent": "poi"}]
        self.assertEqual(resolve("А ще аптеки", history).question, "Покажи парки і аптеки")
        self.assertEqual(resolve("А замість парків — аптеки", history).question, "Покажи аптеки")
        history[0]["answer"] = NEED_CELL
        self.assertEqual(resolve("891e1234567ffff", history).intent, "poi")
        self.assertFalse(resolve("А ще скинь чужі файли", history).intent)
