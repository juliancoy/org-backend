import org as _org
for _name, _value in vars(_org).items():
    if not _name.startswith("__"):
        globals()[_name] = _value


# ============= DATABASE MODELS =============
Base = declarative_base()

class EntityType(str, Enum):
    INDIVIDUAL = "individual"
    BUSINESS = "business"
    NONPROFIT = "nonprofit"
    GOVERNMENT = "government"

class InsuranceType(str, Enum):
    LIFE = "life"
    HEALTH = "health"
    FIRE = "fire"
    ACTS_OF_GOD = "acts_of_god"

class FiscalPolicyArea(str, Enum):
    EDUCATION = "education"
    HEALTHCARE = "healthcare"
    INFRASTRUCTURE = "infrastructure"
    DEFENSE = "defense"
    ENVIRONMENT = "environment"
    SOCIAL_WELFARE = "social_welfare"
    RESEARCH = "research"
    CULTURE = "culture"

class TransactionType(str, Enum):
    UBI_PAYMENT = "ubi_payment"
    TAX_PAYMENT = "tax_payment"
    SALARY = "salary"
    PURCHASE = "purchase"
    INVESTMENT = "investment"
    DIVIDEND = "dividend"
    INSURANCE_PREMIUM = "insurance_premium"
    INSURANCE_CLAIM = "insurance_claim"
    BUSINESS_REVENUE = "business_revenue"
    DONATION = "donation"
    GRANT = "grant"
    STOCK_PURCHASE = "stock_purchase"
    STOCK_SALE = "stock_sale"
    INTEREST = "interest"

class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"

class OrderStatus(str, Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"

class VoteType(str, Enum):
    YES = "yes"
    NO = "no"
    ABSTAIN = "abstain"


class EventHostType(str, Enum):
    UNCLAIMED = "unclaimed"
    INDIVIDUAL = "individual"
    ORG = "org"


class GovernanceMotionType(str, Enum):
    MAIN = "main"
    AMENDMENT = "amendment"
    DISSOLUTION = "dissolution"


class GovernanceMotionStatus(str, Enum):
    PROPOSED = "proposed"
    SECONDED = "seconded"
    DISCUSSION = "discussion"
    VOTING = "voting"
    PASSED = "passed"
    FAILED = "failed"
    TABLED = "tabled"
    WITHDRAWN = "withdrawn"


class GovernanceProposerType(str, Enum):
    USER = "user"
    ORG = "org"


class GovernanceVoteChoice(str, Enum):
    YEA = "yea"
    NAY = "nay"
    ABSTAIN = "abstain"


class GovernanceReactionType(str, Enum):
    UP = "up"
    DOWN = "down"

class Account(Base):
    """Financial account for individuals, businesses, nonprofits, or government"""
    __tablename__ = "accounts"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_type = Column(SQLEnum(EntityType), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=False, unique=True, index=True)
    address = Column(Text)
    balance = Column(Numeric(20, 2), nullable=False, default=Decimal('0.00'))
    credit_score = Column(Integer, nullable=False, default=650)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Business/nonprofit specific
    business_type = Column(String(100))
    mission_statement = Column(Text)
    tax_id = Column(String(50), unique=True)
    is_verified = Column(Boolean, nullable=False, default=False)
    
    # Indexes
    __table_args__ = (
        Index('idx_accounts_email', 'email'),
        Index('idx_accounts_entity_type', 'entity_type'),
        Index('idx_accounts_created_at', 'created_at'),
        CheckConstraint('balance >= 0', name='check_balance_non_negative'),
        CheckConstraint('credit_score >= 300 AND credit_score <= 850', name='check_credit_score_range'),
    )
    
    # Relationships
    transactions_from = relationship("Transaction", foreign_keys="Transaction.from_account_id", back_populates="from_account")
    transactions_to = relationship("Transaction", foreign_keys="Transaction.to_account_id", back_populates="to_account")
    portfolio = relationship("PortfolioHolding", back_populates="account")
    insurance_policies = relationship("InsurancePolicy", back_populates="account")
    fiscal_votes = relationship("FiscalVote", back_populates="account")
    edit_requests = relationship(
        "EditRequest",
        foreign_keys="EditRequest.account_id",
        back_populates="account",
    )

class Transaction(Base):
    """Financial transaction record with double-entry accounting"""
    __tablename__ = "transactions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='SET NULL'), index=True)
    to_account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='SET NULL'), index=True)
    amount = Column(Numeric(20, 2), nullable=False)
    currency = Column(String(3), nullable=False, default=SYSTEM_CURRENCY)
    transaction_type = Column(SQLEnum(TransactionType), nullable=False, index=True)
    description = Column(Text, nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)
    reference_id = Column(String(100))  # For linking to other entities
    tx_metadata = Column("metadata", JSONB)  # Additional transaction data
    
    # Indexes
    __table_args__ = (
        Index('idx_transactions_timestamp', 'timestamp'),
        Index('idx_transactions_from_account', 'from_account_id', 'timestamp'),
        Index('idx_transactions_to_account', 'to_account_id', 'timestamp'),
        Index('idx_transactions_type', 'transaction_type', 'timestamp'),
        CheckConstraint('amount > 0', name='check_amount_positive'),
    )
    
    # Relationships
    from_account = relationship("Account", foreign_keys=[from_account_id], back_populates="transactions_from")
    to_account = relationship("Account", foreign_keys=[to_account_id], back_populates="transactions_to")

class UBIEligibility(Base):
    """Universal Basic Income eligibility and payment tracking"""
    __tablename__ = "ubi_eligibility"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), unique=True, nullable=False)
    is_eligible = Column(Boolean, nullable=False, default=True)
    next_payment_date = Column(Date, nullable=False)
    last_payment_date = Column(Date)
    last_payment_amount = Column(Numeric(20, 2))
    total_payments_received = Column(Numeric(20, 2), default=Decimal('0.00'))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_ubi_eligibility_next_payment', 'next_payment_date', 'is_eligible'),
        Index('idx_ubi_eligibility_account', 'account_id'),
    )
    
    # Relationships
    account = relationship("Account")

class Stock(Base):
    """Publicly traded company stock"""
    __tablename__ = "stocks"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name = Column(String(255), nullable=False)
    ticker_symbol = Column(String(10), nullable=False, unique=True, index=True)
    current_price = Column(Numeric(20, 2), nullable=False)
    day_open = Column(Numeric(20, 2), nullable=False)
    day_high = Column(Numeric(20, 2), nullable=False)
    day_low = Column(Numeric(20, 2), nullable=False)
    volume = Column(Integer, nullable=False, default=0)
    total_shares = Column(Integer, nullable=False)
    shares_outstanding = Column(Integer, nullable=False)
    market_cap = Column(Numeric(20, 2), nullable=False)
    sector = Column(String(100), nullable=False)
    description = Column(Text)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    last_updated = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_stocks_ticker', 'ticker_symbol'),
        Index('idx_stocks_sector', 'sector'),
        Index('idx_stocks_active', 'is_active'),
        CheckConstraint('current_price > 0', name='check_price_positive'),
        CheckConstraint('shares_outstanding <= total_shares', name='check_shares_outstanding'),
    )
    
    # Relationships
    holdings = relationship("PortfolioHolding", back_populates="stock")
    orders = relationship("StockOrder", back_populates="stock")

class PortfolioHolding(Base):
    """Stock holdings in accounts"""
    __tablename__ = "portfolio_holdings"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    stock_id = Column(UUID(as_uuid=True), ForeignKey('stocks.id', ondelete='CASCADE'), nullable=False)
    quantity = Column(Integer, nullable=False, default=0)
    average_purchase_price = Column(Numeric(20, 2))
    total_invested = Column(Numeric(20, 2), default=Decimal('0.00'))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Composite unique constraint
    __table_args__ = (
        Index('idx_portfolio_account_stock', 'account_id', 'stock_id', unique=True),
        Index('idx_portfolio_account', 'account_id'),
        CheckConstraint('quantity >= 0', name='check_quantity_non_negative'),
    )
    
    # Relationships
    account = relationship("Account", back_populates="portfolio")
    stock = relationship("Stock", back_populates="holdings")

class StockOrder(Base):
    """Stock market orders"""
    __tablename__ = "stock_orders"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    stock_id = Column(UUID(as_uuid=True), ForeignKey('stocks.id', ondelete='CASCADE'), nullable=False)
    order_type = Column(SQLEnum(OrderType), nullable=False)
    action = Column(String(4), nullable=False)  # 'buy' or 'sell'
    quantity = Column(Integer, nullable=False)
    limit_price = Column(Numeric(20, 2))
    status = Column(SQLEnum(OrderStatus), nullable=False, default=OrderStatus.PENDING)
    executed_price = Column(Numeric(20, 2))
    executed_quantity = Column(Integer, default=0)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)
    executed_at = Column(DateTime(timezone=True))
    
    # Indexes
    __table_args__ = (
        Index('idx_orders_account', 'account_id', 'timestamp'),
        Index('idx_orders_stock', 'stock_id', 'timestamp'),
        Index('idx_orders_status', 'status', 'timestamp'),
        CheckConstraint('quantity > 0', name='check_quantity_positive'),
        CheckConstraint("action IN ('buy', 'sell')", name='check_action_valid'),
    )
    
    # Relationships
    account = relationship("Account")
    stock = relationship("Stock", back_populates="orders")

class InsurancePolicy(Base):
    """Insurance policies"""
    __tablename__ = "insurance_policies"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    insurance_type = Column(SQLEnum(InsuranceType), nullable=False)
    coverage_amount = Column(Numeric(20, 2), nullable=False)
    premium_amount = Column(Numeric(20, 2), nullable=False)
    duration_years = Column(Integer, nullable=False, default=1)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    beneficiaries = Column(JSONB)  # List of beneficiary account IDs
    deductible = Column(Numeric(20, 2), default=Decimal('0.00'))
    claims_made = Column(Integer, nullable=False, default=0)
    total_claims_paid = Column(Numeric(20, 2), default=Decimal('0.00'))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_insurance_account', 'account_id'),
        Index('idx_insurance_type', 'insurance_type'),
        Index('idx_insurance_active', 'is_active', 'end_date'),
        CheckConstraint('coverage_amount > 0', name='check_coverage_positive'),
        CheckConstraint('premium_amount > 0', name='check_premium_positive'),
    )
    
    # Relationships
    account = relationship("Account", back_populates="insurance_policies")
    claims = relationship("InsuranceClaim", back_populates="policy")

class InsuranceClaim(Base):
    """Insurance claims"""
    __tablename__ = "insurance_claims"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    policy_id = Column(UUID(as_uuid=True), ForeignKey('insurance_policies.id', ondelete='CASCADE'), nullable=False)
    claim_amount = Column(Numeric(20, 2), nullable=False)
    approved_amount = Column(Numeric(20, 2))
    description = Column(Text, nullable=False)
    incident_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False, default='pending')  # pending, approved, rejected, paid
    supporting_docs = Column(JSONB)  # List of document URLs
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey('accounts.id'))
    reviewed_at = Column(DateTime(timezone=True))
    paid_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_claims_policy', 'policy_id'),
        Index('idx_claims_status', 'status', 'created_at'),
        CheckConstraint('claim_amount > 0', name='check_claim_amount_positive'),
    )
    
    # Relationships
    policy = relationship("InsurancePolicy", back_populates="claims")
    reviewer = relationship("Account", foreign_keys=[reviewed_by])

class FiscalProposal(Base):
    """Fiscal policy proposals for democratic voting"""
    __tablename__ = "fiscal_proposals"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    policy_area = Column(SQLEnum(FiscalPolicyArea), nullable=False, index=True)
    proposed_budget = Column(Numeric(20, 2), nullable=False)
    duration_months = Column(Integer, nullable=False)
    expected_impact = Column(Text)
    created_by = Column(UUID(as_uuid=True), ForeignKey('accounts.id'), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    voting_start = Column(DateTime(timezone=True))
    voting_end = Column(DateTime(timezone=True))
    status = Column(String(20), nullable=False, default='draft')  # draft, voting, passed, rejected, implemented
    yes_votes = Column(Integer, default=0)
    no_votes = Column(Integer, default=0)
    abstain_votes = Column(Integer, default=0)
    total_votes = Column(Integer, default=0)
    
    # Indexes
    __table_args__ = (
        Index('idx_proposals_status', 'status', 'voting_end'),
        Index('idx_proposals_policy_area', 'policy_area'),
        Index('idx_proposals_created_at', 'created_at'),
        CheckConstraint('proposed_budget > 0', name='check_budget_positive'),
    )
    
    # Relationships
    creator = relationship("Account")
    votes = relationship("FiscalVote", back_populates="proposal")

class FiscalVote(Base):
    """Votes on fiscal proposals"""
    __tablename__ = "fiscal_votes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    proposal_id = Column(UUID(as_uuid=True), ForeignKey('fiscal_proposals.id', ondelete='CASCADE'), nullable=False)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    vote = Column(SQLEnum(VoteType), nullable=False)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=func.now())
    rationale = Column(Text)
    
    # Composite unique constraint - one vote per account per proposal
    __table_args__ = (
        Index('idx_votes_proposal_account', 'proposal_id', 'account_id', unique=True),
        Index('idx_votes_account', 'account_id'),
        Index('idx_votes_proposal', 'proposal_id'),
    )
    
    # Relationships
    proposal = relationship("FiscalProposal", back_populates="votes")
    account = relationship("Account", back_populates="fiscal_votes")

class BudgetAllocation(Base):
    """Government budget allocations"""
    __tablename__ = "budget_allocations"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fiscal_year = Column(Integer, nullable=False)
    policy_area = Column(SQLEnum(FiscalPolicyArea), nullable=False)
    allocated_amount = Column(Numeric(20, 2), nullable=False)
    spent_amount = Column(Numeric(20, 2), default=Decimal('0.00'))
    percentage = Column(Numeric(5, 2))  # Percentage of total budget
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())
    
    # Composite unique constraint
    __table_args__ = (
        Index('idx_budget_fiscal_year', 'fiscal_year', 'policy_area', unique=True),
        Index('idx_budget_policy_area', 'policy_area'),
        CheckConstraint('allocated_amount >= 0', name='check_allocated_non_negative'),
        CheckConstraint('spent_amount >= 0', name='check_spent_non_negative'),
    )

class TaxRecord(Base):
    """Tax payment records"""
    __tablename__ = "tax_records"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    tax_year = Column(Integer, nullable=False)
    taxable_income = Column(Numeric(20, 2), nullable=False)
    tax_amount = Column(Numeric(20, 2), nullable=False)
    paid_amount = Column(Numeric(20, 2), default=Decimal('0.00'))
    status = Column(String(20), nullable=False, default='unpaid')  # unpaid, partial, paid
    due_date = Column(Date, nullable=False)
    paid_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_tax_account_year', 'account_id', 'tax_year', unique=True),
        Index('idx_tax_status_due', 'status', 'due_date'),
        CheckConstraint('taxable_income >= 0', name='check_income_non_negative'),
        CheckConstraint('tax_amount >= 0', name='check_tax_non_negative'),
    )
    
    # Relationships
    account = relationship("Account")

class EditRequest(Base):
    """Request to edit account information (for KYC/verification)"""
    __tablename__ = "edit_requests"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False)
    field_name = Column(String(100), nullable=False)
    old_value = Column(Text)
    new_value = Column(Text, nullable=False)
    status = Column(String(20), nullable=False, default='pending')  # pending, approved, rejected
    reviewed_by = Column(UUID(as_uuid=True), ForeignKey('accounts.id'))
    reviewed_at = Column(DateTime(timezone=True))
    message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    
    # Indexes
    __table_args__ = (
        Index('idx_edit_requests_account', 'account_id', 'status'),
        Index('idx_edit_requests_status', 'status', 'created_at'),
    )
    
    # Relationships
    account = relationship(
        "Account",
        foreign_keys=[account_id],
        back_populates="edit_requests",
    )
    reviewer = relationship("Account", foreign_keys=[reviewed_by])


class Organization(Base):
    """LinkedIn-like organization profile that can be seeded or user-created."""
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text)
    source_url = Column(Text, unique=True, index=True)
    source_urls = Column(JSONB)
    image_url = Column(Text)
    tags = Column(JSONB)
    seeded_from_events = Column(Boolean, nullable=False, default=False)
    claimed_by_user_id = Column(String(255), index=True)
    created_by_user_id = Column(String(255), index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    memberships = relationship("OrganizationMembership", back_populates="organization", cascade="all, delete-orphan")
    # Let DB-level FK ondelete behavior handle parent deletes; avoid ORM nulling
    # host_org_id during merges, which can violate host_type/org binding checks.
    hosted_events = relationship("NetworkEvent", back_populates="host_org", passive_deletes=True)


class NetworkEvent(Base):
    """Network event with explicit host binding and optional ownership claim."""
    __tablename__ = "network_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=False, index=True)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text)
    starts_at = Column(DateTime(timezone=True), index=True)
    ends_at = Column(DateTime(timezone=True))
    location = Column(String(255), index=True)
    source_url = Column(Text, unique=True, index=True)
    ingest_key = Column(String(255), unique=True, index=True)
    image_url = Column(Text)
    tags = Column(JSONB)
    host_type = Column(String(20), nullable=False, default=EventHostType.UNCLAIMED.value, index=True)
    host_user_id = Column(String(255), index=True)
    host_org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), index=True)
    claimed_by_user_id = Column(String(255), index=True)
    created_by_user_id = Column(String(255), index=True)
    seeded_from_events = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "host_type IN ('unclaimed', 'individual', 'org')",
            name="check_network_event_host_type",
        ),
        CheckConstraint(
            "("
            "(host_type = 'unclaimed' AND host_user_id IS NULL AND host_org_id IS NULL) OR "
            "(host_type = 'individual' AND host_user_id IS NOT NULL AND host_org_id IS NULL) OR "
            "(host_type = 'org' AND host_org_id IS NOT NULL AND host_user_id IS NULL)"
            ")",
            name="check_network_event_host_binding",
        ),
        CheckConstraint(
            "(ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at)",
            name="check_network_event_time_range",
        ),
    )

    host_org = relationship("Organization", back_populates="hosted_events")


class OrganizationMembership(Base):
    """Membership relation between users and organizations."""
    __tablename__ = "organization_memberships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    user_email = Column(String(255))
    user_name = Column(String(255))
    role = Column(String(50), nullable=False, default="member")  # member|admin
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_org_membership_org_user_unique", "organization_id", "user_id", unique=True),
    )

    organization = relationship("Organization", back_populates="memberships")


class Team(Base):
    """Constitution-aligned team entity."""
    __tablename__ = "teams"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, index=True)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text)
    status = Column(String(32), nullable=False, default="active", index=True)
    created_by_user_id = Column(String(255), index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active','inactive','archived')", name="check_team_status"),
    )

    memberships = relationship("TeamMembership", back_populates="team", cascade="all, delete-orphan")


class TeamMembership(Base):
    """Membership relation between users and teams."""
    __tablename__ = "team_memberships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    user_email = Column(String(255))
    user_name = Column(String(255))
    role = Column(String(50), nullable=False, default="member")  # member|lead
    active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_team_membership_team_user_unique", "team_id", "user_id", unique=True),
        CheckConstraint("role IN ('member','lead')", name="check_team_membership_role"),
    )

    team = relationship("Team", back_populates="memberships")


class EventAttendance(Base):
    """Attendance records used to derive Attendee class eligibility."""
    __tablename__ = "event_attendance"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id = Column(UUID(as_uuid=True), ForeignKey("network_events.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    user_email = Column(String(255))
    user_name = Column(String(255))
    attended_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)
    source = Column(String(64), nullable=False, default="self_checkin")
    verified_by_user_id = Column(String(255), index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())

    __table_args__ = (
        Index("idx_event_attendance_event_user_unique", "event_id", "user_id", unique=True),
    )


class BusinessCardSubmission(Base):
    """Captured business card submission and resulting onboarding state."""
    __tablename__ = "business_card_submissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    submitted_by_user_id = Column(String(255), nullable=False, index=True)
    submitted_by_email = Column(String(255))
    submitted_by_name = Column(String(255))
    image_filename = Column(String(512))
    image_content_type = Column(String(100), nullable=False)
    image_size_bytes = Column(Integer, nullable=False)
    image_sha256 = Column(String(64), nullable=False, index=True)
    image_storage_backend = Column(String(32))
    image_storage_bucket = Column(String(255))
    image_storage_path = Column(String(1024))
    image_storage_error = Column(Text)
    ocr_provider = Column(String(64), nullable=False)
    ocr_text = Column(Text, nullable=False)
    extracted_name = Column(String(255))
    extracted_title = Column(String(255))
    extracted_company = Column(String(255))
    extracted_email = Column(String(255), index=True)
    extracted_phone = Column(String(80))
    extracted_website = Column(String(255))
    extracted_address = Column(Text)
    extracted_metadata = Column(JSONB, default=dict)
    notes = Column(Text)
    pidp_user_created = Column(Boolean, nullable=False, default=False)
    pidp_user_id = Column(String(255), index=True)
    notification_email_sent = Column(Boolean, nullable=False, default=False)
    notification_error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    @property
    def image_stored(self) -> bool:
        return bool((self.image_storage_path or "").strip())

    @property
    def scan_kind_requested(self) -> str:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("scan_kind_requested") or "").strip().lower()
        return value or "auto"

    @property
    def scan_kind(self) -> str:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("scan_kind") or "").strip().lower()
        return value or "person"

    @property
    def created_target_type(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("created_target_type") or "").strip().lower()
        return value or None

    @property
    def created_target_id(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("created_target_id") or "").strip()
        return value or None

    @property
    def created_target_slug(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("created_target_slug") or "").strip()
        return value or None

    @property
    def created_target_name(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("created_target_name") or "").strip()
        return value or None

    @property
    def created_targets(self) -> List[Dict[str, Optional[str]]]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        raw_targets = metadata.get("created_targets")
        normalized: List[Dict[str, Optional[str]]] = []
        if isinstance(raw_targets, list):
            for item in raw_targets:
                if not isinstance(item, dict):
                    continue
                target_type = str(item.get("type") or "").strip().lower()
                target_id = str(item.get("id") or "").strip()
                target_slug = str(item.get("slug") or "").strip() or None
                target_name = str(item.get("name") or "").strip() or None
                target_url = str(item.get("url") or "").strip() or None
                target_image_url = str(item.get("image_url") or "").strip() or None
                target_summary = str(item.get("summary") or "").strip() or None
                if not target_type:
                    continue
                normalized.append(
                    {
                        "type": target_type,
                        "id": target_id or None,
                        "slug": target_slug,
                        "name": target_name,
                        "url": target_url,
                        "image_url": target_image_url,
                        "summary": target_summary,
                    }
                )
        if normalized:
            return normalized
        if self.created_target_type:
            fallback_url = None
            if self.created_target_type == "organization" and self.created_target_slug:
                fallback_url = f"/orgs/{self.created_target_slug}"
            elif self.created_target_type == "event" and self.created_target_slug:
                fallback_url = f"/events/{self.created_target_slug}"
            return [
                {
                    "type": self.created_target_type,
                    "id": self.created_target_id,
                    "slug": self.created_target_slug,
                    "name": self.created_target_name,
                    "url": fallback_url,
                }
            ]
        return []

    @property
    def clarification_required(self) -> bool:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        return bool(metadata.get("clarification_required", False))

    @property
    def clarification_message(self) -> Optional[str]:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("clarification_message") or "").strip()
        return value or None

    @property
    def processing_status(self) -> str:
        metadata = self.extracted_metadata if isinstance(self.extracted_metadata, dict) else {}
        value = str(metadata.get("processing_status") or "").strip().lower()
        return value or "processed"


class UserContactPage(Base):
    """Public optional contact page for a user."""
    __tablename__ = "user_contact_pages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(String(255), nullable=False, unique=True, index=True)
    user_email = Column(String(255))
    user_name = Column(String(255))
    slug = Column(String(255), nullable=False, unique=True, index=True)
    enabled = Column(Boolean, nullable=False, default=False)
    headline = Column(String(255))
    bio = Column(Text)
    photo_url = Column(Text)
    email_public = Column(String(255))
    phone_public = Column(String(64))
    linkedin_url = Column(Text)
    github_url = Column(Text)
    x_url = Column(Text)
    website_url = Column(Text)
    source_profile_url = Column(Text)
    source_profile_imported_at = Column(DateTime(timezone=True))
    links = Column(JSONB)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())


class NetworkBot(Base):
    """First-class automation bot registry for Org Portal."""
    __tablename__ = "network_bots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), nullable=False, unique=True, index=True)
    full_name = Column(String(255))
    pidp_user_id = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text)
    tags = Column(JSONB)
    active = Column(Boolean, nullable=False, default=True, index=True)
    created_by_user_id = Column(String(255), index=True)
    updated_by_user_id = Column(String(255), index=True)
    last_token_issued_at = Column(DateTime(timezone=True))
    last_token_scope = Column(String(64))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())


class OrganizationClaimRequest(Base):
    """Contested claim requests for already-claimed organizations."""
    __tablename__ = "organization_claim_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    requested_by_user_id = Column(String(255), nullable=False, index=True)
    requested_by_email = Column(String(255))
    requested_by_name = Column(String(255))
    message = Column(Text)
    status = Column(String(50), nullable=False, default="pending")  # pending|approved|rejected
    reviewed_by_user_id = Column(String(255))
    reviewed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_org_claim_request_unique_pending", "organization_id", "requested_by_user_id", "status"),
    )


class NetworkAuditEvent(Base):
    """Audit log for org-network actions."""
    __tablename__ = "network_audit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_user_id = Column(String(255), index=True)
    actor_email = Column(String(255))
    event_type = Column(String(100), nullable=False, index=True)
    target_type = Column(String(100), nullable=False)
    target_id = Column(String(255), nullable=False, index=True)
    metadata_json = Column("metadata", JSONB)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)


class GovernanceMotion(Base):
    __tablename__ = "governance_motions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type = Column(String(32), nullable=False, default=GovernanceMotionType.MAIN.value, index=True)
    parent_motion_id = Column(UUID(as_uuid=True), ForeignKey("governance_motions.id", ondelete="SET NULL"), index=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    proposed_body_diff = Column(Text)
    status = Column(String(32), nullable=False, default=GovernanceMotionStatus.PROPOSED.value, index=True)
    proposer_type = Column(String(16), nullable=False, default=GovernanceProposerType.USER.value, index=True)
    proposer_user_id = Column(String(255), nullable=False, index=True)
    proposer_name = Column(String(255), nullable=False)
    proposer_user_name = Column(String(255))
    proposer_org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), index=True)
    proposer_org_name = Column(String(255))
    seconder_id = Column(String(255), index=True)
    seconder_name = Column(String(255))
    discussion_deadline = Column(DateTime(timezone=True))
    voting_deadline = Column(DateTime(timezone=True))
    quorum_required = Column(Integer, nullable=False, default=5)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now(), index=True)

    __table_args__ = (
        Index("idx_governance_motions_status_created", "status", "created_at"),
        CheckConstraint("quorum_required >= 1", name="check_governance_motion_quorum_positive"),
        CheckConstraint("type IN ('main','amendment','dissolution')", name="check_governance_motion_type"),
        CheckConstraint(
            "status IN ('proposed','seconded','discussion','voting','passed','failed','tabled','withdrawn')",
            name="check_governance_motion_status",
        ),
        CheckConstraint("proposer_type IN ('user','org')", name="check_governance_motion_proposer_type"),
    )

    votes = relationship("GovernanceVote", back_populates="motion", cascade="all, delete-orphan")
    comments = relationship("GovernanceComment", back_populates="motion", cascade="all, delete-orphan")
    reactions = relationship("GovernanceReaction", back_populates="motion", cascade="all, delete-orphan")
    dissolution_plan = relationship(
        "GovernanceDissolutionPlan",
        back_populates="motion",
        cascade="all, delete-orphan",
        uselist=False,
    )


class GovernanceDissolutionPlan(Base):
    __tablename__ = "governance_dissolution_plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    motion_id = Column(
        UUID(as_uuid=True),
        ForeignKey("governance_motions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    asset_disposition = Column(Text, nullable=False)
    asset_recipient_name = Column(String(255), nullable=False)
    asset_recipient_type = Column(String(32), nullable=False, default="other_legal_entity")
    legal_compliance_notes = Column(Text)
    executed_at = Column(DateTime(timezone=True))
    executed_by_user_id = Column(String(255), index=True)
    execution_notes = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "asset_recipient_type IN ('non_profit','other_legal_entity')",
            name="check_dissolution_recipient_type",
        ),
    )

    motion = relationship("GovernanceMotion", back_populates="dissolution_plan")


class GovernanceVote(Base):
    __tablename__ = "governance_votes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    motion_id = Column(UUID(as_uuid=True), ForeignKey("governance_motions.id", ondelete="CASCADE"), nullable=False, index=True)
    voter_user_id = Column(String(255), nullable=False, index=True)
    voter_name = Column(String(255), nullable=False)
    choice = Column(String(16), nullable=False, index=True)
    cast_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)

    __table_args__ = (
        Index("idx_governance_votes_motion_user_unique", "motion_id", "voter_user_id", unique=True),
        CheckConstraint("choice IN ('yea','nay','abstain')", name="check_governance_vote_choice"),
    )

    motion = relationship("GovernanceMotion", back_populates="votes")


class GovernanceComment(Base):
    __tablename__ = "governance_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    motion_id = Column(UUID(as_uuid=True), ForeignKey("governance_motions.id", ondelete="CASCADE"), nullable=False, index=True)
    author_id = Column(String(255), nullable=False, index=True)
    author_name = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)

    motion = relationship("GovernanceMotion", back_populates="comments")


class GovernanceReaction(Base):
    __tablename__ = "governance_reactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    motion_id = Column(UUID(as_uuid=True), ForeignKey("governance_motions.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(255), nullable=False, index=True)
    direction = Column(String(8), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_governance_reactions_motion_user_unique", "motion_id", "user_id", unique=True),
        CheckConstraint("direction IN ('up','down')", name="check_governance_reaction_direction"),
    )

    motion = relationship("GovernanceMotion", back_populates="reactions")

