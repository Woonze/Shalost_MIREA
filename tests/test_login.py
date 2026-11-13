import unittest
from playwright.sync_api import sync_playwright
import main


class LoginStateTests(unittest.TestCase):
    def test_accented_name_and_login_form(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.route("https://pulse.mirea.ru/**", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
            page.goto("https://pulse.mirea.ru/services")
            page.set_content('<nav>Мои занятия Сервисы <a>Ту\u0301р К. А.</a></nav>')
            state = page.evaluate(main.STATE_JS)
            self.assertTrue(state["authenticatedUI"])
            self.assertEqual(state["name"], "Тур")
            page.set_content('<nav>Мои занятия Сервисы <a>Ту\u0301р К. А.</a></nav><input type="password">')
            self.assertFalse(page.evaluate(main.STATE_JS)["authenticatedUI"])
            page.set_content('<nav>Мои занятия Сервисы <a>Настройки</a></nav>')
            self.assertFalse(page.evaluate(main.STATE_JS)["authenticatedUI"])
            browser.close()


if __name__ == "__main__":
    unittest.main()
