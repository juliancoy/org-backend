import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


# ============= PYDANTIC MODELS =============

class AccountCreate(BaseModel):
    entity_type: EntityType
    name: str = Field(..., min_length=1, max_length=255)
    email: str = Field(..., pattern=r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    address: Optional[str] = None
    business_type: Optional[str] = None
    mission_statement: Optional[str] = None
    initial_deposit: Decimal = Field(Decimal('0.00'), ge=0)

class AccountUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    address: Optional[str] = None
    business_type: Optional[str] = None
    mission_statement: Optional[str] = None

class AccountResponse(BaseModel):
    id: uuid.UUID
    entity_type: EntityType
    name: str
    email: str
    address: Optional[str]
    balance: Decimal
    credit_score: int
    created_at: datetime
    business_type: Optional[str] = None
    mission_statement: Optional[str] = None
    tax_id: Optional[str] = None
    is_verified: bool
    
    model_config = ConfigDict(from_attributes=True)

class AccountListItemResponse(BaseModel):
    id: uuid.UUID
    entity_type: EntityType
    name: str
    email: str
    balance: Decimal
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class MoneySupplyPointResponse(BaseModel):
    timestamp: datetime
    total_supply: Decimal

class MoneySupplyHistoryResponse(BaseModel):
    points: List[MoneySupplyPointResponse]
    current_total_supply: Decimal
    currency: str

class TransactionCreate(BaseModel):
    to_account_id: Optional[uuid.UUID] = None
    amount: Decimal = Field(..., gt=0)
    transaction_type: TransactionType
    description: str
    reference_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class TransactionResponse(BaseModel):
    id: uuid.UUID
    from_account_id: Optional[uuid.UUID]
    to_account_id: Optional[uuid.UUID]
    amount: Decimal
    currency: str
    transaction_type: TransactionType
    description: str
    timestamp: datetime
    reference_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(None, alias="tx_metadata")
    
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

class RecentTransactionResponse(BaseModel):
    id: uuid.UUID
    timestamp: datetime
    transaction_type: TransactionType
    amount: Decimal
    currency: str
    description: str
    from_account_id: Optional[uuid.UUID] = None
    to_account_id: Optional[uuid.UUID] = None
    from_account_name: Optional[str] = None
    to_account_name: Optional[str] = None

class AccountAutomationResponse(BaseModel):
    account_id: uuid.UUID
    name: str
    email: str
    balance: Decimal
    currency: str
    account_endpoint: str
    incoming_transactions_endpoint: str
    all_transactions_endpoint: str
    send_payment_endpoint: str
    send_url_template: str
    updated_at: datetime

class StockCreate(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=255)
    ticker_symbol: str = Field(..., min_length=1, max_length=10, pattern=r'^[A-Z]{1,10}$')
    total_shares: int = Field(..., gt=0)
    initial_price: Decimal = Field(..., gt=0)
    sector: str
    description: Optional[str] = None

class StockOrderCreate(BaseModel):
    stock_id: uuid.UUID
    quantity: int = Field(..., gt=0)
    order_type: OrderType
    limit_price: Optional[Decimal] = Field(None, gt=0)
    action: str = Field(..., pattern='^(buy|sell)$')

class InsurancePolicyCreate(BaseModel):
    insurance_type: InsuranceType
    coverage_amount: Decimal = Field(..., gt=0)
    duration_years: int = Field(1, ge=1, le=30)
    beneficiaries: Optional[List[uuid.UUID]] = None
    deductible: Optional[Decimal] = Field(None, ge=0)

class InsuranceClaimCreate(BaseModel):
    policy_id: uuid.UUID
    claim_amount: Decimal = Field(..., gt=0)
    description: str
    incident_date: date
    supporting_docs: Optional[List[str]] = None

class FiscalProposalCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str
    policy_area: FiscalPolicyArea
    proposed_budget: Decimal = Field(..., gt=0)
    duration_months: int = Field(..., gt=0, le=120)
    expected_impact: str
    voting_days: int = Field(7, ge=1, le=30)

class FiscalVoteCreate(BaseModel):
    vote: VoteType
    rationale: Optional[str] = None

class TaxEstimate(BaseModel):
    taxable_income: Decimal = Field(..., ge=0)
    tax_year: int

class UBIRuntimeSettingsResponse(BaseModel):
    interval_seconds: int
    dena_annual: Decimal
    dena_precision: int
    entity_types: List[str]
    updated_at: datetime
    updated_by: Optional[str] = None

class UBIRuntimeSettingsUpdate(BaseModel):
    interval_seconds: Optional[int] = Field(None, ge=1, le=86400)
    dena_annual: Optional[Decimal] = Field(None, ge=Decimal("0"))
    dena_precision: Optional[int] = Field(None, ge=0, le=12)
    entity_types: Optional[List[str]] = None

    @field_validator("entity_types")
    @classmethod
    def validate_entity_types(cls, value):
        if value is None:
            return value
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("entity_types must contain at least one value")
        return cleaned


class BusinessCardAbuseSettingsResponse(BaseModel):
    enabled: bool
    per_user_limit_per_hour: int
    per_ip_limit_per_hour: int
    global_limit_per_hour: int
    duplicate_hash_limit: int
    duplicate_hash_window_seconds: int
    max_bytes: int
    allowed_content_types: List[str]
    event_link_enrichment_enabled: bool = True
    auto_clarification_enabled: bool = True
    auto_min_confidence: float = 0.75
    auto_min_margin: float = 0.2
    updated_at: datetime
    updated_by: Optional[str] = None


class BusinessCardAbuseSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    per_user_limit_per_hour: Optional[int] = Field(None, ge=1, le=2000)
    per_ip_limit_per_hour: Optional[int] = Field(None, ge=1, le=10000)
    global_limit_per_hour: Optional[int] = Field(None, ge=1, le=50000)
    duplicate_hash_limit: Optional[int] = Field(None, ge=1, le=100)
    duplicate_hash_window_seconds: Optional[int] = Field(None, ge=60, le=30 * 24 * 3600)
    max_bytes: Optional[int] = Field(None, ge=1024 * 100, le=25 * 1024 * 1024)
    allowed_content_types: Optional[List[str]] = None
    event_link_enrichment_enabled: Optional[bool] = None
    auto_clarification_enabled: Optional[bool] = None
    auto_min_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    auto_min_margin: Optional[float] = Field(None, ge=0.0, le=1.0)

    @field_validator("allowed_content_types")
    @classmethod
    def validate_allowed_content_types(cls, value):
        if value is None:
            return value
        cleaned = sorted({item.strip().lower() for item in value if item and item.strip()})
        if not cleaned:
            raise ValueError("allowed_content_types must contain at least one value")
        invalid = [item for item in cleaned if "/" not in item or not item.startswith("image/")]
        if invalid:
            raise ValueError(f"Only image/* content types are allowed: {', '.join(invalid)}")
        return cleaned


class OrganizationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None
    claim_on_create: bool = True


class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None


class OrganizationMembershipUpsert(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    role: str = Field("member", pattern="^(member|admin)$")


class OrganizationMembershipUpdate(BaseModel):
    role: str = Field(..., pattern="^(member|admin)$")


class OrganizationClaimRequestCreate(BaseModel):
    message: Optional[str] = Field(None, max_length=4000)


class OrganizationMergeRequest(BaseModel):
    source_organization_id: uuid.UUID


class OrganizationClaimRequestResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    requested_by_user_id: str
    requested_by_email: Optional[str] = None
    requested_by_name: Optional[str] = None
    message: Optional[str] = None
    status: str
    reviewed_by_user_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrganizationClaimRequestQueueItemResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    organization_name: str
    organization_slug: str
    organization_claimed_by_user_id: Optional[str] = None
    requested_by_user_id: str
    requested_by_email: Optional[str] = None
    requested_by_name: Optional[str] = None
    message: Optional[str] = None
    status: str
    reviewed_by_user_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime


class OrganizationMembershipResponse(BaseModel):
    user_id: str
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    role: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None


class TeamMembershipUpsert(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    role: str = Field("member", pattern="^(member|lead)$")
    active: bool = True


class TeamMembershipResponse(BaseModel):
    user_id: str
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    role: str
    active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str] = None
    status: str
    created_by_user_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EventAttendanceRecordResponse(BaseModel):
    id: uuid.UUID
    event_id: uuid.UUID
    user_id: str
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    attended_at: datetime
    source: str
    verified_by_user_id: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BusinessCardSubmissionResponse(BaseModel):
    id: uuid.UUID
    submitted_by_user_id: str
    submitted_by_name: Optional[str] = None
    image_filename: Optional[str] = None
    image_content_type: str
    image_size_bytes: int
    image_stored: bool = False
    ocr_provider: str
    extracted_name: Optional[str] = None
    extracted_title: Optional[str] = None
    extracted_company: Optional[str] = None
    extracted_email: Optional[str] = None
    extracted_phone: Optional[str] = None
    extracted_website: Optional[str] = None
    extracted_address: Optional[str] = None
    scan_kind_requested: str = "auto"
    scan_kind: str = "person"
    created_target_type: Optional[str] = None
    created_target_id: Optional[str] = None
    created_target_slug: Optional[str] = None
    created_target_name: Optional[str] = None
    created_targets: List[Dict[str, Optional[str]]] = Field(default_factory=list)
    clarification_required: bool = False
    clarification_message: Optional[str] = None
    processing_status: str = "processed"
    extracted_metadata: Dict[str, Any] = Field(default_factory=dict)
    pidp_user_created: bool
    pidp_user_id: Optional[str] = None
    notification_email_sent: bool
    notification_error: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MatrixBootstrapSessionResponse(BaseModel):
    access_token: str
    user_id: str
    device_id: Optional[str] = None
    homeserver_url: str


class OrgChatRoomDirectoryItemResponse(BaseModel):
    organization_id: uuid.UUID
    organization_name: str
    organization_slug: str
    relationship_status: str = Field(..., pattern="^(attendee|member|admin)$")
    organization_member_count: int = 0
    room_id: str
    room_alias: Optional[str] = None
    room_name: Optional[str] = None


class ChatLinkPreviewResponse(BaseModel):
    url: str
    canonical_url: str
    title: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    site_name: Optional[str] = None
    domain: Optional[str] = None


class AccessClassSnapshotResponse(BaseModel):
    is_public: bool
    is_attendee: bool
    is_member: bool
    is_org_admin: bool
    is_sysadmin: bool
    reasons: List[str] = Field(default_factory=list)


class OrganizationResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    source_urls: List[str] = Field(default_factory=list)
    image_url: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    seeded_from_events: bool
    claimed_by_user_id: Optional[str] = None
    created_by_user_id: Optional[str] = None
    membership_count: int = 0
    my_role: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PublicOrganizationResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    source_urls: List[str] = Field(default_factory=list)
    image_url: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    seeded_from_events: bool
    upcoming_events_count: int = 0
    pending_claim_requests_count: int = 0
    is_contested: bool = False
    redirected_from_slug: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PublicOrganizationListItemResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: Optional[str] = None
    source_url: Optional[str] = None
    source_urls: List[str] = Field(default_factory=list)
    image_url: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    seeded_from_events: bool
    membership_count: int = 0
    upcoming_events_count: int = 0
    pending_claim_requests_count: int = 0
    is_contested: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PublicOrganizationAdminResponse(BaseModel):
    user_id: str
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    role: str = "admin"


class PublicOrganizationChatResponse(BaseModel):
    organization_slug: str
    room_exists: bool = False
    room_id: Optional[str] = None
    room_alias: Optional[str] = None
    room_name: Optional[str] = None


class PublicOrganizationChatMessageResponse(BaseModel):
    event_id: str
    sender: Optional[str] = None
    body: str
    sent_at: Optional[datetime] = None


class PublicOrganizationChatRoomFeedResponse(BaseModel):
    key: str
    label: str
    room_id: Optional[str] = None
    room_alias: Optional[str] = None
    room_name: Optional[str] = None
    messages: List[PublicOrganizationChatMessageResponse] = Field(default_factory=list)


class PublicOrganizationChatFeedResponse(BaseModel):
    organization_slug: str
    rooms: List[PublicOrganizationChatRoomFeedResponse] = Field(default_factory=list)


class PublicEventChatResponse(BaseModel):
    event_slug: str
    room_exists: bool = False
    room_id: Optional[str] = None
    room_alias: Optional[str] = None
    room_name: Optional[str] = None
    messages: List[PublicOrganizationChatMessageResponse] = Field(default_factory=list)


class OrgChatRoomBackfillResponse(BaseModel):
    organizations_total: int
    organizations_scanned: int
    public_rooms_found: int
    public_rooms_created: int
    announcements_rooms_found: int
    announcements_rooms_created: int
    errors: List[str] = Field(default_factory=list)


class NetworkEventCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    location: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None
    host_type: str = Field(EventHostType.UNCLAIMED.value, pattern="^(unclaimed|individual|org)$")
    host_user_id: Optional[str] = Field(None, min_length=1, max_length=255)
    host_org_id: Optional[uuid.UUID] = None
    claim_on_create: bool = False


class NetworkEventClaimRequest(BaseModel):
    host_type: str = Field(..., pattern="^(individual|org)$")
    host_user_id: Optional[str] = Field(None, min_length=1, max_length=255)
    host_org_id: Optional[uuid.UUID] = None


class NetworkEventResponse(BaseModel):
    id: uuid.UUID
    title: str
    slug: str
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    location: Optional[str] = None
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    host_type: str
    host_user_id: Optional[str] = None
    host_org_id: Optional[uuid.UUID] = None
    host_org_name: Optional[str] = None
    claimed_by_user_id: Optional[str] = None
    created_by_user_id: Optional[str] = None
    seeded_from_events: bool
    represented_in_codecollective_source: bool
    is_unclaimed: bool
    my_host_role: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GovernanceMotionCreate(BaseModel):
    type: str = Field(GovernanceMotionType.MAIN.value, pattern="^(main|amendment|dissolution)$")
    parent_motion_id: Optional[uuid.UUID] = None
    title: str = Field(..., min_length=1, max_length=255)
    body: str = Field(..., min_length=1)
    proposed_body_diff: Optional[str] = None
    proposer_type: str = Field(GovernanceProposerType.USER.value, pattern="^(user|org)$")
    proposer_org_id: Optional[uuid.UUID] = None
    quorum_required: int = Field(5, ge=1, le=100000)
    dissolution_asset_disposition: Optional[str] = None
    dissolution_asset_recipient_name: Optional[str] = None
    dissolution_asset_recipient_type: Optional[str] = Field(
        None,
        pattern="^(non_profit|other_legal_entity)$",
    )
    dissolution_legal_compliance_notes: Optional[str] = None


class NetworkEventPublicFeedResponse(BaseModel):
    generated_at: datetime
    total: int
    events: List[NetworkEventResponse] = Field(default_factory=list)


class GovernanceMotionResponse(BaseModel):
    id: uuid.UUID
    type: str
    parent_motion_id: Optional[uuid.UUID] = None
    title: str
    body: str
    proposed_body_diff: Optional[str] = None
    status: str
    proposer_type: str
    proposer_id: str
    proposer_name: str
    proposer_user_name: Optional[str] = None
    proposer_org_id: Optional[uuid.UUID] = None
    proposer_org_name: Optional[str] = None
    seconder_id: Optional[str] = None
    seconder_name: Optional[str] = None
    discussion_deadline: Optional[datetime] = None
    voting_deadline: Optional[datetime] = None
    quorum_required: int
    is_dissolution: bool = False
    created_at: datetime
    updated_at: datetime


class GovernanceMotionVoteCastRequest(BaseModel):
    choice: str = Field(..., pattern="^(yea|nay|abstain)$")


class GovernanceCommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10000)


class GovernanceCommentResponse(BaseModel):
    id: uuid.UUID
    motion_id: uuid.UUID
    author_id: str
    author_name: str
    body: str
    created_at: datetime


class GovernanceReactionResponse(BaseModel):
    score: int
    user_vote: Optional[str] = None


class GovernanceVoteCountsResponse(BaseModel):
    up: int = 0
    down: int = 0
    score: int = 0


class GovernanceUserVoteResponse(BaseModel):
    user_vote: Optional[str] = None


class GovernanceVoteResultResponse(BaseModel):
    yea: int = 0
    nay: int = 0
    abstain: int = 0
    total_eligible: int = 0
    participating_voters: int = 0
    threshold_rule: str = "simple_majority"
    required_yea: int = 0
    quorum_met: bool = False
    passed: bool = False


class GovernanceDissolutionPlanResponse(BaseModel):
    motion_id: uuid.UUID
    asset_disposition: str
    asset_recipient_name: str
    asset_recipient_type: str
    legal_compliance_notes: Optional[str] = None
    executed_at: Optional[datetime] = None
    executed_by_user_id: Optional[str] = None
    execution_notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GovernanceDissolutionExecuteRequest(BaseModel):
    execution_notes: Optional[str] = Field(None, max_length=5000)


class CalendarIngestOrganization(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = None
    image_url: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    city: Optional[str] = Field(None, max_length=64)


class CalendarIngestEvent(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None
    location: Optional[str] = Field(None, max_length=255)
    source_url: Optional[str] = None
    host_org_source_url: Optional[str] = None
    host_org_name: Optional[str] = Field(None, max_length=255)
    host_org_image_url: Optional[str] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None
    city: Optional[str] = Field(None, max_length=64)
    ingest_key: Optional[str] = Field(None, max_length=255)


class CalendarIngestPayload(BaseModel):
    source: Optional[str] = Field("genCalendar", max_length=120)
    run_id: Optional[str] = Field(None, max_length=255)
    generated_at: Optional[datetime] = None
    organizations: List[CalendarIngestOrganization] = Field(default_factory=list)
    events: List[CalendarIngestEvent] = Field(default_factory=list)


class CalendarIngestResponse(BaseModel):
    organizations_inserted: int
    organizations_updated: int
    events_inserted: int
    events_updated: int
    events_skipped: int


class SeedOrganizationsResponse(BaseModel):
    loaded: int
    inserted: int
    updated: int


class ContactLink(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    url: str = Field(..., min_length=1, max_length=1000)


class ContactPageUpdate(BaseModel):
    enabled: Optional[bool] = None
    slug: Optional[str] = Field(None, min_length=1, max_length=255)
    headline: Optional[str] = Field(None, max_length=255)
    bio: Optional[str] = None
    photo_url: Optional[str] = None
    email_public: Optional[str] = None
    phone_public: Optional[str] = Field(None, max_length=64)
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    x_url: Optional[str] = None
    website_url: Optional[str] = None
    links: Optional[List[ContactLink]] = None

    @field_validator("email_public")
    @classmethod
    def _validate_email_public(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if not cleaned:
            return None
        if not re.fullmatch(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}$", cleaned):
            raise ValueError("email_public must be a valid email address")
        return cleaned

    @field_validator("phone_public")
    @classmethod
    def _validate_phone_public(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        normalized = re.sub(r"[\s().-]+", "", cleaned)
        if normalized.startswith("+"):
            digits = normalized[1:]
            candidate = f"+{digits}"
        else:
            digits = normalized
            candidate = digits
        if not digits.isdigit() or len(digits) < 7 or len(digits) > 15:
            raise ValueError("phone_public must contain 7-15 digits")
        return candidate


class ContactPageResponse(BaseModel):
    user_id: str
    user_name: str
    slug: str
    enabled: bool
    headline: Optional[str] = None
    bio: Optional[str] = None
    photo_url: Optional[str] = None
    email_public: Optional[str] = None
    phone_public: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    x_url: Optional[str] = None
    website_url: Optional[str] = None
    source_profile_url: Optional[str] = None
    source_profile_imported_at: Optional[datetime] = None
    links: List[ContactLink] = Field(default_factory=list)
    public_url: Optional[str] = None
    updated_at: datetime


class PublicUserProfileResponse(BaseModel):
    user_id: str
    user_name: str
    slug: str
    headline: Optional[str] = None
    bio: Optional[str] = None
    photo_url: Optional[str] = None
    email_public: Optional[str] = None
    phone_public: Optional[str] = None
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    x_url: Optional[str] = None
    website_url: Optional[str] = None
    links: List[ContactLink] = Field(default_factory=list)
    public_url: Optional[str] = None
    upcoming_events_count: int = 0
    updated_at: datetime


class PublicUserListItemResponse(BaseModel):
    user_id: str
    user_name: str
    slug: str
    headline: Optional[str] = None
    photo_url: Optional[str] = None
    upcoming_events_count: int = 0
    updated_at: datetime


class NetworkUserListItemResponse(BaseModel):
    user_id: str
    user_name: str
    email: str
    created_at: datetime
    contact_slug: Optional[str] = None
    contact_enabled: bool = False
    headline: Optional[str] = None
    photo_url: Optional[str] = None


class NetworkBotResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: Optional[str] = None
    pidp_user_id: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    active: bool
    created_by_user_id: Optional[str] = None
    updated_by_user_id: Optional[str] = None
    last_token_issued_at: Optional[datetime] = None
    last_token_scope: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NetworkBotProvisionRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    full_name: Optional[str] = Field(None, max_length=255)
    password: str = Field(..., min_length=8, max_length=255)
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    issue_api_token: bool = True
    api_token_name: str = Field("orgportal-bot", min_length=1, max_length=100)
    api_token_scope: str = Field("org_admin", pattern="^(service|org_portal|org_mcp|org_admin)$")


class NetworkBotProvisionResponse(BaseModel):
    bot: NetworkBotResponse
    issued_api_token: Optional[str] = None
    issued_api_token_name: Optional[str] = None
    issued_api_token_scope: Optional[str] = None


class NetworkBotIssueTokenRequest(BaseModel):
    password: str = Field(..., min_length=8, max_length=255)
    api_token_name: str = Field("orgportal-bot", min_length=1, max_length=100)
    api_token_scope: str = Field("org_admin", pattern="^(service|org_portal|org_mcp|org_admin)$")


class NetworkBotIssueTokenResponse(BaseModel):
    bot: NetworkBotResponse
    issued_api_token: str
    issued_api_token_name: str
    issued_api_token_scope: str


class ContactImportPayload(BaseModel):
    source_url: str = Field(..., min_length=1, max_length=1000)
    overwrite: bool = True


class ContactImportResponse(BaseModel):
    contact: ContactPageResponse
    imported_fields: List[str] = Field(default_factory=list)
    source_url: str


