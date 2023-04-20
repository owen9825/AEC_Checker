#!python3
import io
import os
from typing import Dict, Optional, Tuple

from selenium import webdriver
import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.select import Select
from selenium.common.exceptions import NoSuchElementException
import csv
import argparse
import collections
from enum import Enum
from webdriver_manager.firefox import GeckoDriverManager
import sys


class AECResult(Enum):
    PASS = "Pass"
    PARTIAL = "Partial"
    FAIL = "Fail"
    FAIL_STREET = "Fail_Street"
    FAIL_SUBURB = "Fail_Suburb"


AECStatus = collections.namedtuple(
    "AECStatus", ["result", "federal", "state", "local_gov", "local_ward"]
)

ADDRESSES = {
    "address1",
    "address2",
    "address3",
    "city",  # Suburb
    "state",
    "zip",  # Postal code
    "country_code"
}

PRIMARY_ADDRESSES = [f"primary_{val}" for val in ADDRESSES]

EXPECTED_FIELDS = {
    "first_name",
    "middle_name",
    "last_name",
    "nationbuilder_id"
}.union(PRIMARY_ADDRESSES)


def get_given_names(membership_row: Dict[str, Optional[str]]):
    return (membership_row["first_name"] + " " + membership_row["middle_name"]).strip()


def get_address_components(row: Dict[str, Optional[str]]) -> Tuple[
    Optional[str],Optional[str], Optional[str], Optional[str]]:
    """
    For a membership row, return the street name, suburb, state, and postcode.
    """
    street_words = []
    for word in row["primary_address1"].split():
        if street_words or len(word.strip("0123456789")) == len(word):
            street_words.append(word)
    street_name = " ".join(street_words)
    return street_name, row["primary_city"], row["primary_state"], row["primary_zip"]


CAPTCHA_INPUT_ID = "textVerificationCode"


def focus_on_captcha_input(driver: webdriver):
    driver.find_element(
        By.ID, CAPTCHA_INPUT_ID
    ).send_keys("")


def getAECStatus(
    driver: webdriver,
    membership_row: Dict[str, Optional[str]]
) -> AECStatus:
    given_names = get_given_names(membership_row)
    street, suburb, state, postcode = get_address_components(membership_row)
    if not postcode or not postcode.isnumeric():
        print(f"{given_names} lacks a postcode, so we lack valid details for them")
        return AECStatus(
            AECResult.FAIL, None, None, None, None
        )
    elem = driver.find_element(By.ID, "ctl00_ContentPlaceHolderBody_textGivenName")
    elem.clear()
    elem.send_keys(given_names)

    elem = driver.find_element(By.ID, "ctl00_ContentPlaceHolderBody_textSurname")
    elem.clear()
    elem.send_keys(membership_row["last_name"])
    print(f"Considering {given_names} {membership_row['last_name']} ({membership_row['nationbuilder_id']})")

    elem = driver.find_element(By.ID, "ctl00_ContentPlaceHolderBody_textPostcode")
    elem.clear()
    elem.send_keys(postcode)

    time.sleep(0.1)

    suburb_state = f"{str.upper(suburb)} ({state})"
    try:
        suburb_dropdown = Select(
            driver.find_element(By.ID, "ctl00_ContentPlaceHolderBody_DropdownSuburb")
        )
        suburb_dropdown.select_by_value(suburb_state)
    except Exception as e:
        print(e, suburb_state, file=sys.stderr)
        return AECStatus(AECResult.FAIL_SUBURB, "", "", "", "")

    elem = driver.find_element(By.ID, "ctl00_ContentPlaceHolderBody_textStreetName")
    elem.clear()
    elem.send_keys(street)

    captcha_entered = False
    focus_on_captcha_input(driver)

    while not captcha_entered:
        try:
            elem = driver.find_element(
                By.ID, CAPTCHA_INPUT_ID
            )
        except NoSuchElementException:
            print("The browser must have been commandeered")
            break

        if len(elem.get_attribute("value")) == 4:
            driver.find_element(
                By.ID, "ctl00_ContentPlaceHolderBody_buttonVerify"
            ).click()

            try:
                # Look for the first name tag, if it exist the captcha failed
                driver.find_element(By.ID, "ctl00_ContentPlaceHolderBody_textGivenName")
            except Exception:
                # Otherwise we're good. (why is a success state in an exception, brah)
                captcha_entered = True

            if not captcha_entered:
                focus_on_captcha_input(driver)
        else:
            time.sleep(1)

    try:
        driver.find_element(By.ID, "ctl00_ContentPlaceHolderBody_panelSuccess")

        federal_division = ""
        state_district = ""
        local_gov = ""
        local_ward = ""

        try:
            federal_division = driver.find_element(
                By.ID, "ctl00_ContentPlaceHolderBody_linkProfile"
            ).text
            state_district = driver.find_element(
                By.ID, "ctl00_ContentPlaceHolderBody_labelStateDistrict2"
            ).text
            local_gov = driver.find_element(
                By.ID, "ctl00_ContentPlaceHolderBody_labelLGA2"
            ).text
            local_ward = driver.find_element(
                By.ID, "ctl00_ContentPlaceHolderBody_labelLGAWard2"
            ).text
        except Exception:
            pass

        driver.find_element(By.ID, "ctl00_ContentPlaceHolderBody_buttonBack").click()
        return AECStatus(
            AECResult.PASS, federal_division, state_district, local_gov, local_ward
        )

    except Exception:
        out = AECStatus(AECResult.FAIL, "", "", "", "")
        try:
            reason = driver.find_element(
                By.ID, "ctl00_ContentPlaceHolderBody_labelFailedReason"
            )
            if "partial" in reason.text:
                out = AECStatus(AECResult.PARTIAL, "", "", "", "")
            elif "street" in reason.text:
                out = AECStatus(AECResult.FAIL_STREET, "", "", "", "")
        except Exception:
            pass
        driver.find_element(
            By.ID, "ctl00_ContentPlaceHolderBody_buttonTryAgain"
        ).click()
        return out


OUTPUT_FIELDS = ["first_name", "middle_name", "last_name", "nationbuilder_id",
                 "AEC_result", "federal_division", "state_division", "local_government", "local_ward"]


def get_driver():
    # https://github.com/SergeyPirogov/webdriver_manager#use-with-firefox
    driver = webdriver.Firefox(executable_path=GeckoDriverManager().install())
    return driver


def check_rows(input_filename, output_filename, skip: int):
    with get_driver() as driver:
        driver.get("https://check.aec.gov.au/")
        with io.open(input_filename) as csvfile:
            reader = csv.DictReader(csvfile, delimiter=",")
            if not EXPECTED_FIELDS.issubset(reader.fieldnames):
                raise ValueError(f"Some fields are missing from this file: one of {', '.join(EXPECTED_FIELDS)}")
            row_count = 0
            existing_output = os.path.exists(output_filename)
            with io.open(
                output_filename,
                "a",
                newline="",
            ) as output_file:
                writer = csv.DictWriter(output_file, fieldnames=OUTPUT_FIELDS)
                if not existing_output:
                    writer.writeheader()
                for membership_row in reader:
                    row_count += 1
                    output_row = {k: membership_row.get(k) for k in OUTPUT_FIELDS}
                    if row_count <= skip:
                        # Assume that this has already been written as output.
                        continue
                    if not membership_row["first_name"]:
                        # A member needs a  name
                        continue
                    time.sleep(0.1)
                    status = getAECStatus(driver, membership_row)
                    output_row.update({"AEC_result": status[0],
                                       "federal_division": status[1],
                                       "state_division": status[2],
                                       "local_government": status[3],
                                       "local_ward": status[4]})
                    writer.writerow(output_row)


def main():
    parser = argparse.ArgumentParser(
        description="This program will iterate through a CSV file of members, and submit their details into the AEC "
                    "website, to confirm their enrollment details.")
    parser.add_argument(
        "--skip", type=int, default=0, help="skip entries you've already seen"
    )
    parser.add_argument("--infile", default="input.csv",
                        help="This file is presumed to be exported from NationBuilder, with fields such as 'first_name'"
                             " and 'primary_address1'")
    parser.add_argument("--outfile", default="output.csv")
    args = parser.parse_args()
    check_rows(args.infile, args.outfile, args.skip)


if __name__ == "__main__":
    main()
