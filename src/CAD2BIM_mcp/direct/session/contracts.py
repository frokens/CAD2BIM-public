"""Direct session provider response contracts."""

from pydantic import Field

from CAD2BIM_mcp.direct.contract_base import DirectContract


class DirectSessionResult(DirectContract):
    direct_session_alias: str = Field(pattern=r"^direct_[0-9]{3,}$")
    session_name: str = Field(min_length=1, max_length=100)
    resumed: bool
