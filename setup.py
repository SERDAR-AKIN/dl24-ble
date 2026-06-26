from setuptools import setup, find_packages

with open("requirements.txt") as f:
    requirements = [line.strip() for line in f if line.strip() and not line.startswith("#")]

setup(
    name="dl24-ble",
    version="1.0.0",
    description="Atorch DL24 Electronic Load BLE Controller for Linux",
    packages=find_packages(include=["dl24_ble", "dl24_ble.*", "cli", "cli.*"]),
    python_requires=">=3.9,<3.14",
    install_requires=requirements,
    entry_points={
        "console_scripts": [
            "dl24=cli.main:main",
        ],
    },
)
