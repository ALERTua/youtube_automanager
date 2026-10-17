"""The user agent of each request that the service sends to YouTube and Google.

The cookies come from a Firefox export, so each request with them names that browser. yt-dlp would send a random
Chrome for each process. This module imports no yt-dlp, so the process of the service can use it for the rotation.
"""

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
