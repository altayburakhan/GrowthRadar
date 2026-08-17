from patchright.sync_api import Error as PlaywrightError

from growthradar.js_network import ToolCategory, detect_tools


def test_ai_assistant_tools_are_excluded_from_onboarding_by_default_category_check() -> None:
    # Sanity check that the new category is distinct from onboarding/analytics
    # -- scoring.py's category != "onboarding" filter relies on this.
    assert ToolCategory.AI_ASSISTANT != ToolCategory.ONBOARDING
    assert ToolCategory.AI_ASSISTANT != ToolCategory.ANALYTICS


class _FailingPage:
    url = "https://broken.example.com"

    def evaluate(self, script: str, *args: object, **kwargs: object) -> None:
        raise PlaywrightError("evaluation context destroyed")


def test_detect_tools_never_raises_on_evaluation_failure() -> None:
    detections = detect_tools(_FailingPage(), [])  # type: ignore[arg-type]
    assert detections == []
