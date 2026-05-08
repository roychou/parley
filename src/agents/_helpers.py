from datetime import datetime

def build_system_prompt(role_description: str) -> str:
    datestr = datetime.now().strftime("%Y-%m-%d")
    return f"{role_description}\n\nToday's date is {datestr}."