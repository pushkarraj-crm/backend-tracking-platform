def workspace_output(workspace):
    return {
        k: getattr(workspace, k)
        for k in (
            "id",
            "name",
            "account_type",
            "active",
            "plan",
            "subscription_status",
            "trial_ends_at",
            "paid_until",
            "created_at",
        )
    }
