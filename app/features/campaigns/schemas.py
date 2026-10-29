from decimal import Decimal
from typing import Literal

from pydantic import Field, HttpUrl, model_validator

from app.features.campaigns.policy import CatalogSettings, EventBody, ProfileBody
from app.features.tracking.rules import Rules
from app.shared.schemas import OfferCreate, StrictModel


class CampaignBody(OfferCreate):
    settings: CatalogSettings = Field(default_factory=CatalogSettings)
    tracking_rules: Rules = Field(default_factory=Rules)
    active: bool = True
    campaign_model: Literal["CPL", "CPS"] = "CPL"
    sale_percentage: Decimal | None = Field(
        default=None, gt=0, le=100, max_digits=7, decimal_places=4
    )
    description: str = Field(default="", max_length=10000)
    # Funnel stages (e.g. approved, disbursed) created together with the campaign.
    events: list[EventBody] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def rate(self):
        from app.features.campaigns.policy import safe_url

        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Campaign name cannot be blank")
        safe_url(self.destination_url, templates=True)
        ProfileBody(
            name=self.name,
            description=self.description,
            destination_url=self.destination_url,
            settings=self.settings,
        )
        if self.destination_url.scheme != "https":
            raise ValueError("Campaign destinations must use HTTPS")
        if self.campaign_model == "CPS" and self.sale_percentage is None:
            raise ValueError("CPS campaigns require a sale commission percentage")
        if self.campaign_model == "CPL" and self.sale_percentage is not None:
            raise ValueError("Only CPS campaigns use sale commission percentages")
        if len({e.code for e in self.events}) != len(self.events):
            raise ValueError("Each funnel stage needs a unique code")
        return self


class ApplicationBody(StrictModel):
    message: str = Field(default="", max_length=1000)


class CloneBody(StrictModel):
    name: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_name(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Campaign name cannot be blank")
        return self


class ReviewBody(StrictModel):
    status: Literal["approved", "rejected"]
    note: str = Field(default="", max_length=1000)


class PublisherPostbackBody(StrictModel):
    url: HttpUrl
