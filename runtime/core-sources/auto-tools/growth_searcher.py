"""
成长路径搜索器 v6.0 — 为"人变强"设计的搜索策略
不搜AI工具，搜：这个领域最强的人是怎么做到的
"""


# 角色→搜索策略映射
GROWTH_SEARCH_STRATEGIES = {
    "代码开发者": {
        "ceiling_queries": [
            "indie hacker $10K MRR journey",
            "solo developer successful product launch story",
            "from engineer to product builder transition",
        ],
        "path_queries": [
            "developer side project to paid product steps",
            "technical founder first 10 customers how",
            "shipping mindset vs building mindset developer",
        ],
        "mindset_queries": [
            "why most developers never ship products",
            "engineering excellence vs product thinking",
            "developer to founder mental model shift",
        ],
    },
    "内容创作者": {
        "ceiling_queries": [
            "content creator million subscriber strategy",
            "newsletter 10000 subscribers playbook",
            "personal brand building creator economy",
        ],
        "path_queries": [
            "from anonymous to known content creator journey",
            "content creator first 1000 true fans",
            "writing consistently for 2 years what I learned",
        ],
        "mindset_queries": [
            "content creation consistency vs quality debate",
            "why most creators fail at monetization",
            "audience building vs content creation priority",
        ],
    },
    "商业创业者": {
        "ceiling_queries": [
            "micro SaaS founder first paying customer",
            "validate business idea in one weekend",
            "solopreneur $100K ARR real story",
        ],
        "path_queries": [
            "from idea to revenue fastest path",
            "lean startup validation without code",
            "first customer acquisition strategies bootstrapped",
        ],
        "mindset_queries": [
            "why ideas are worthless execution matters",
            "founder market fit self assessment",
            "entrepreneurial thinking vs employee thinking",
        ],
    },
    "量化投研者": {
        "ceiling_queries": [
            "quantitative fund research process breakdown",
            "systematic trading from backtest to live",
            "Renaissance Technologies methodology insights",
        ],
        "path_queries": [
            "quant researcher career progression path",
            "retail quant to professional fund transition",
            "building quantitative edge as individual",
        ],
        "mindset_queries": [
            "overfitting in quant research psychology",
            "risk management mental models for traders",
            "why most quant strategies fail in production",
        ],
    },
    "知识管理者": {
        "ceiling_queries": [
            "Tiago Forte building second brain methodology",
            "knowledge creator to thought leader path",
            "personal knowledge management to paid products",
        ],
        "path_queries": [
            "from note taking to original thinking",
            "knowledge synthesis skills development",
            "building intellectual reputation online",
        ],
        "mindset_queries": [
            "collector vs creator knowledge worker",
            "why more input doesnt equal more output",
            "from consuming to producing knowledge shift",
        ],
    },
    "学习研究者": {
        "ceiling_queries": [
            "Andrej Karpathy research communication style",
            "PhD to industry thought leader transition",
            "research impact beyond citations",
        ],
        "path_queries": [
            "from paper reader to paper writer journey",
            "finding original research questions methodology",
            "building research reputation early career",
        ],
        "mindset_queries": [
            "research taste development in machine learning",
            "breadth vs depth in AI research career",
            "when to follow vs create research directions",
        ],
    },
    "Agent进化者": {
        "ceiling_queries": [
            "AI agent architecture best practices 2026",
            "Harrison Chase LangChain design philosophy",
            "from agent user to framework contributor",
        ],
        "path_queries": [
            "AI engineer to AI architect career path",
            "building open source AI tools recognition",
            "from using frameworks to designing them",
        ],
        "mindset_queries": [
            "agent reliability vs agent capability tradeoff",
            "why most agent demos fail in production",
            "engineering mindset for AI systems design",
        ],
    },
    "信息推送者": {
        "ceiling_queries": [
            "Ben Thompson Stratechery business model",
            "paid newsletter creator economy success",
            "information curation as a profession",
        ],
        "path_queries": [
            "from information aggregator to analyst",
            "building trust as information source",
            "newsletter growth from 0 to 10K subscribers",
        ],
        "mindset_queries": [
            "information value vs information volume",
            "prediction accuracy as reputation builder",
            "why curation is undervalued skill",
        ],
    },
    "AI陪伴者": {
        "ceiling_queries": [
            "character AI top creator strategies",
            "AI companion design emotional depth",
            "human AI relationship design principles",
        ],
        "path_queries": [
            "from AI chatbot to meaningful AI companion",
            "designing AI characters with personality",
            "AI experience designer career emerging",
        ],
        "mindset_queries": [
            "authenticity in AI interactions design",
            "emotional intelligence in AI product design",
            "beyond chatbot AI companion differentiation",
        ],
    },
    "生活助理使用者": {
        "ceiling_queries": [
            "Tim Ferriss life design methodology",
            "personal automation life optimization",
            "intentional living with technology balance",
        ],
        "path_queries": [
            "from productivity tools to life design",
            "automation for wellbeing not just efficiency",
            "designing personal systems that stick",
        ],
        "mindset_queries": [
            "productivity trap doing more vs living better",
            "technology servant vs technology master",
            "essentialism in digital age practical",
        ],
    },
}


def get_growth_queries(role: str, query_type: str = "all") -> list:
    """获取角色对应的成长搜索查询

    Args:
        role: 用户角色（如"代码开发者"）
        query_type: "ceiling"（天花板）/ "path"（路径）/ "mindset"（心智）/ "all"

    Returns:
        搜索查询列表
    """
    # 模糊匹配角色
    strategy = None
    for key, val in GROWTH_SEARCH_STRATEGIES.items():
        if key in role or role in key:
            strategy = val
            break

    if not strategy:
        # fallback
        return [
            f"{role} 领域顶尖从业者的成长路径",
            f"how to become top in {role} field",
            f"{role} career progression milestones",
        ]

    if query_type == "ceiling":
        return strategy["ceiling_queries"]
    elif query_type == "path":
        return strategy["path_queries"]
    elif query_type == "mindset":
        return strategy["mindset_queries"]
    else:
        # 每类取1-2个混合
        return (
            strategy["ceiling_queries"][:1] +
            strategy["path_queries"][:1] +
            strategy["mindset_queries"][:1]
        )
