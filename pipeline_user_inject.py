"""
Open WebUI Pipeline Function — 注入用户身份到请求体

在 Open WebUI 管理后台 → 工作区 → Functions → 新建 Filter，
把这段代码粘贴进去即可。

作用：每次聊天请求发给适配层之前，自动把 user_id/name/email/role
注入到请求体中，省去适配层每次调 Open WebUI API 查用户信息。
"""


class Filter:
    def __init__(self):
        pass

    def inlet(self, body: dict, __user__: dict) -> dict:
        """请求发给适配层之前，注入用户身份"""
        body["user_id"] = __user__.get("id", "")
        body["user_name"] = __user__.get("name", "")
        body["user_email"] = __user__.get("email", "")
        body["user_role"] = __user__.get("role", "user")
        return body
