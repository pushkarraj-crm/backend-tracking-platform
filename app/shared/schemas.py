from decimal import Decimal
from typing import Literal

import phonenumbers
import pycountry
from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(StrictModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class Register(Credentials):
    name: str = Field(min_length=2, max_length=120)
    company: str = Field(min_length=1, max_length=120)
    business_type: Literal[
        "brand_direct_advertiser",
        "agency",
        "affiliate_ad_network",
        "mobile_apps",
        "media_buyer",
        "social_media_advertising",
        "influencer_creator",
    ]
    usage_types: list[
        Literal[
            "cpi_app_promotion",
            "lead_generation",
            "e_commerce",
            "email_marketing",
            "traffic_source",
            "finance_fintech",
            "travel_hospitality",
        ]
    ] = Field(min_length=1, max_length=7)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    mobile: str = Field(min_length=7, max_length=25)
    teams_enabled: bool = False
    teams_email: EmailStr | None = None
    preferred_language: Literal["en", "hi", "es", "fr", "ar", "pt", "de"] = "en"
    referral_source: Literal[
        "search_engine",
        "social_media",
        "referral",
        "existing_customer",
        "comparison_site",
        "event",
        "other",
    ]
    terms_accepted: Literal[True]
    legal_version: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def contact_details(self):
        self.name = self.name.strip()
        self.company = self.company.strip()
        if len(self.name) < 2 or not self.company:
            raise ValueError("Name and company cannot be blank")
        if pycountry.countries.get(alpha_2=self.country) is None:
            raise ValueError("Select a valid country")
        raw = "+" + self.mobile[2:] if self.mobile.startswith("00") else self.mobile
        try:
            parsed = phonenumbers.parse(raw, self.country)
        except phonenumbers.NumberParseException:
            raise ValueError("Enter a valid international mobile number")
        if not phonenumbers.is_valid_number(parsed):
            raise ValueError("Enter a valid mobile number")
        self.mobile = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        if self.teams_enabled and not self.teams_email:
            raise ValueError("Microsoft Teams email is required when Teams is selected")
        if not self.teams_enabled and self.teams_email:
            raise ValueError("Select Microsoft Teams to provide a Teams email")
        if len(set(self.usage_types)) != len(self.usage_types):
            raise ValueError("Usage types must be unique")
        return self


class Login(Credentials):
    pass


class UserCreate(Credentials):
    role: Literal["admin", "staff", "affiliate", "advertiser", "publisher"]
    parent_user_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class OfferCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    destination_url: HttpUrl
    revenue: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    payout: Decimal = Field(ge=0, max_digits=14, decimal_places=4)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    advertiser_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    attribution_hours: int = Field(default=720, ge=1, le=2160)

    @model_validator(mode="after")
    def safe_url(self):
        if self.destination_url.username or self.destination_url.password:
            raise ValueError("Destination URL cannot contain credentials")
        if len(str(self.destination_url)) > 2048:
            raise ValueError("Destination URL is too long")
        return self


class LinkCreate(StrictModel):
    offer_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    affiliate_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Postback(StrictModel):
    click_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    transaction_id: str = Field(min_length=1, max_length=120)
    event_type: Literal["lead", "sale"] | None = None
    sale_amount: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=4)
    sale_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    event_key: str = Field(default="initial", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    advertiser_parameters: dict[str, str] = Field(default_factory=dict, max_length=6)

    @model_validator(mode="after")
    def valid_parameters(self):
        allowed = {f"adv_sub{i}" for i in range(1, 6)} | {"coupon"}
        if set(self.advertiser_parameters) - allowed or any(
            len(v) > 256 or any(ord(c) < 32 for c in v) for v in self.advertiser_parameters.values()
        ):
            raise ValueError("Use bounded adv_sub1 through adv_sub5 and coupon parameters")
        return self


class StatusUpdate(StrictModel):
    status: Literal["approved", "rejected"]


class ActiveUpdate(StrictModel):
    active: bool
