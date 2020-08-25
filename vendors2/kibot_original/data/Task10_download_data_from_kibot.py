# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.3.0
#   kernelspec:
#     display_name: Python [conda env:develop] *
#     language: python
#     name: conda-env-develop-py
# ---

# %%
# %load_ext autoreload
# %autoreload 2

# %%
import os
import platform

# %%
import bs4
import matplotlib
# TODO(gp): Create conda package.
import numpy as np
import pandas as pd
import scipy
import seaborn as sns
import sklearn

# %%
import boto3
# %%
import helpers.io_ as io_
import helpers.printing as print_
import helpers.s3 as hs3

print("python=", platform.python_version())

print("numpy=", np.__version__)
print("pandas=", pd.__version__)
print("seaborn=", sns.__version__)
print("scipy=", scipy.__version__)
print("matplotlib=", matplotlib.__version__)
print("sklearn=", sklearn.__version__)


print_.config_notebook()


s3_resource = boto3.resource("s3")

# %% [markdown]
# Below is the list of all products you have ordered so far:
#
# 1. All Futures Continuous Contracts tick on 3/21/2019       Download...
# 2. All Futures Continuous Contracts 1min on 3/21/2019       Download...
# 3. All Futures Continuous Contracts daily on 3/21/2019       Download...
# 4. All Futures Contracts 1min on 7/20/2018       Download...
# 5. All Futures Contracts daily on 7/20/2018       Download...

# %% [markdown]
# # Get list of links to download.

# %%
if False:
    #!wget http://www.kibot.com/downloadtext.aspx?product=1,All_Futures_Contracts_daily -O All_Futures_Contracts_daily.txt
    pass

# %%
# wget 'http://api.kibot.com/?action=download&link=v8v5vuv9vdv43kvmv9vnvuvdvbpkkrvupkvavs3kvzvtvuvtvbvsvnkrvtv4v8v2vjvtvnvuvsv23kk1krvnpkvcvrvdv13k3m363zkcknkrv9v4vuvsvbvlv8v13kv2v8v9v1pkkrvnvuv8vbvuv2v8vuvs3kkckikckikcktktk1krv2v9vbvsv5vu3kkckrv8vuvuv8v5vmvcvsv4vu3kkckrvbvsv7vtv1v8vbvnvsvnvnv9vdv43kk1krvtvnvsvb3kv8vrvsv43pv5vdvcv5v8vnvukjv4vsvukrvav8vnvnv6vdvbv23kvsvzvsk4vsvbvsv7vaaiamal7n7r7n7v' --compression=gzip -qO- | gzip >test.csv.gz

# %%
# Need to save from the browser because of auth issues.
# tag = "All_Futures_Contracts_1min"
tag = "All_Futures_Contracts_Daily"
# All_Futures_Continuous_Contracts_1min.html
# All_Futures_Continuous_Contracts_daily.html
# All_Futures_Continuous_Contracts_tick.html

filename = tag + ".html"
html = io_.from_file(filename)

# %%
soup = bs4.BeautifulSoup(html, "html.parser")

tables = soup.findAll("table")
print(len(tables))

for table in tables:
    if table.findParent("table") is None:
        # print(str(table[:10])))
        # print(table)
        # from IPython.core.display import display, HTML
        # display(HTML(str(table)))
        if table.get("class", "") == ["ms-classic4-main"]:
            print("Found")
            df = pd.read_html(str(table))[0]
            df.columns = df.iloc[0]
            df = df.iloc[1:]
            cols = [
                np.where(tag.has_attr("href"), tag.get("href"), "no link")
                for tag in table.find_all("a")
            ]
            df["Link"] = [str(c) for c in cols]

# %%
df.head()

# %%
df.to_csv(tag + ".csv")

# %%
df.iloc[0]["links"]

file_name = os.path.join(
    hs3.get_path(), "kibot/All_Futures_Contracts_daily/JY.csv.gz"
)

# %% [markdown]
# # Download

# %%
assert 0