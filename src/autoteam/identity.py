"""注册时填表用的随机身份生成器。

目标：降低 OpenAI add-phone 风控触发率。硬编码的 name="User"、生日 1995/06/15
在批量注册时非常显眼，极易被标记。本模块提供类人随机值，纯标准库实现。
"""

import random
import string

# 常见英文名字，故意避开太罕见或太明星化的
_FIRST_NAMES = [
    "Aaron",
    "Abigail",
    "Adam",
    "Alex",
    "Alice",
    "Amelia",
    "Andrew",
    "Anna",
    "Anthony",
    "Ariana",
    "Austin",
    "Ava",
    "Benjamin",
    "Brandon",
    "Brian",
    "Caleb",
    "Carter",
    "Charles",
    "Charlotte",
    "Chloe",
    "Christopher",
    "Claire",
    "Connor",
    "Daniel",
    "David",
    "Dylan",
    "Edward",
    "Elena",
    "Eli",
    "Eliza",
    "Elizabeth",
    "Ella",
    "Ellie",
    "Emily",
    "Emma",
    "Ethan",
    "Evan",
    "Evelyn",
    "Gabriel",
    "George",
    "Grace",
    "Hailey",
    "Hannah",
    "Harper",
    "Henry",
    "Isaac",
    "Isabella",
    "Jack",
    "Jackson",
    "Jacob",
    "James",
    "Jason",
    "Jennifer",
    "Jessica",
    "John",
    "Jonathan",
    "Jordan",
    "Joseph",
    "Joshua",
    "Julia",
    "Justin",
    "Katherine",
    "Kayla",
    "Kevin",
    "Kyle",
    "Laura",
    "Leah",
    "Liam",
    "Lily",
    "Logan",
    "Lucas",
    "Luke",
    "Madison",
    "Mark",
    "Mary",
    "Mason",
    "Matthew",
    "Megan",
    "Mia",
    "Michael",
    "Michelle",
    "Natalie",
    "Nathan",
    "Nicholas",
    "Noah",
    "Olivia",
    "Owen",
    "Patrick",
    "Paul",
    "Peter",
    "Rachel",
    "Rebecca",
    "Robert",
    "Ryan",
    "Sarah",
    "Savannah",
    "Scott",
    "Sean",
    "Sophia",
    "Stephen",
    "Thomas",
    "Tyler",
    "Victoria",
    "William",
    "Zachary",
    "Zoe",
]

_LAST_NAMES = [
    "Adams",
    "Allen",
    "Anderson",
    "Bailey",
    "Baker",
    "Bell",
    "Bennett",
    "Brooks",
    "Brown",
    "Campbell",
    "Carter",
    "Clark",
    "Collins",
    "Cook",
    "Cooper",
    "Cox",
    "Davis",
    "Diaz",
    "Edwards",
    "Evans",
    "Fisher",
    "Ford",
    "Foster",
    "Garcia",
    "Gonzalez",
    "Gray",
    "Green",
    "Hall",
    "Harris",
    "Henderson",
    "Hernandez",
    "Hill",
    "Howard",
    "Hughes",
    "Jackson",
    "James",
    "Jenkins",
    "Johnson",
    "Jones",
    "Kelly",
    "King",
    "Lee",
    "Lewis",
    "Long",
    "Lopez",
    "Martin",
    "Martinez",
    "Mitchell",
    "Moore",
    "Morgan",
    "Morris",
    "Murphy",
    "Myers",
    "Nelson",
    "Nguyen",
    "Parker",
    "Patel",
    "Perez",
    "Peterson",
    "Phillips",
    "Powell",
    "Price",
    "Ramirez",
    "Reed",
    "Reyes",
    "Richardson",
    "Rivera",
    "Roberts",
    "Robinson",
    "Rodriguez",
    "Rogers",
    "Ross",
    "Russell",
    "Sanchez",
    "Sanders",
    "Scott",
    "Simmons",
    "Smith",
    "Stewart",
    "Sullivan",
    "Taylor",
    "Thomas",
    "Thompson",
    "Torres",
    "Turner",
    "Walker",
    "Ward",
    "Washington",
    "Watson",
    "White",
    "Williams",
    "Wilson",
    "Wood",
    "Wright",
    "Young",
]

# 用作密码前缀的类人单词
_PASSWORD_WORDS = [
    "Apple",
    "Beach",
    "Coffee",
    "Dragon",
    "Eagle",
    "Forest",
    "Galaxy",
    "Harbor",
    "Island",
    "Jasper",
    "Kitten",
    "Lotus",
    "Maple",
    "Nebula",
    "Orange",
    "Pepper",
    "Quartz",
    "River",
    "Sunset",
    "Tiger",
    "Umbra",
    "Velvet",
    "Willow",
    "Yellow",
    "Zephyr",
    "Autumn",
    "Breeze",
    "Castle",
    "Diamond",
    "Ember",
    "Falcon",
    "Ginger",
    "Hazel",
    "Indigo",
    "Jungle",
    "Lemon",
    "Mango",
    "Ocean",
    "Panda",
    "Raven",
    "Silver",
    "Thunder",
]


def random_first_name():
    return random.choice(_FIRST_NAMES)


def random_last_name():
    return random.choice(_LAST_NAMES)


def random_full_name():
    """返回 'Emma Wilson' 这种标准两词姓名。"""
    return f"{random_first_name()} {random_last_name()}"


def random_birthday(min_age=22, max_age=42):
    """
    返回字符串形式的 {"year", "month", "day"}，月/日补零到两位。
    默认 22-42 岁范围内，避开 18-21 的"刚成年"区间（OpenAI 对年轻用户风控更严）。
    """
    import datetime as _dt

    today = _dt.date.today()
    age = random.randint(min_age, max_age)
    year = today.year - age
    # 简单起见 day 只取 1-28，避免月末/闰年问题
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return {
        "year": str(year),
        "month": f"{month:02d}",
        "day": f"{day:02d}",
    }


def random_age(min_age=22, max_age=42):
    """返回字符串形式的年龄，和 random_birthday 取值范围一致。"""
    return str(random.randint(min_age, max_age))


def random_password():
    """
    生成类人密码:`Capword` + `lowerword` + 3-4 数字 + 1 特殊符号,保证 ≥12 位。
    OpenAI 当前注册页校验密码至少 12 字符,旧的 `Harbor42!`(9位)会卡在密码输入阶段。
    例:`HarborWillow427!`, `DragonOcean1234#`。
    """
    word1 = random.choice(_PASSWORD_WORDS)
    word2 = random.choice(_PASSWORD_WORDS).lower()
    digits_len = random.choice([3, 3, 4])
    digits = "".join(random.choices(string.digits, k=digits_len))
    symbol = random.choice("!@#$")
    return f"{word1}{word2}{digits}{symbol}"


def random_identity():
    """一次性生成完整身份，确保生日/年龄一致。"""
    import datetime as _dt

    bday = random_birthday()
    # 用 today().year 代替硬编码年份，避免跨年后 age 漂移
    age = str(_dt.date.today().year - int(bday["year"]))
    return {
        "first_name": random_first_name(),
        "last_name": random_last_name(),
        "full_name": None,
        "birthday": bday,
        "age": age,
        "password": random_password(),
    }
