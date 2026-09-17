# Earth Engine ADK agents

In this exercise, build an Earth Engine enabled ADK agent starting from an example in the Earth Engine community Github repository. This is agent building "the hard way," from scratch, using human written tools.

## Prerequisites

Have a Cloud Project with a billing account. WARNING\! This demo consumes billable resources.

Activate these APIs on your project:  
[https://console.cloud.google.com/apis/api/earthengine.googleapis.com/](https://console.cloud.google.com/apis/api/earthengine.googleapis.com/)  
[https://console.cloud.google.com/apis/api/aiplatform.googleapis.com/](https://console.cloud.google.com/apis/api/aiplatform.googleapis.com/)  
[https://console.cloud.google.com/apis/api/serviceusage.googleapis.com/](https://console.cloud.google.com/apis/api/serviceusage.googleapis.com/)  
[https://console.cloud.google.com/apis/api/cloudresourcemanager.googleapis.com/](https://console.cloud.google.com/apis/api/cloudresourcemanager.googleapis.com/)

You may need to register your project with Earth Engine:  
[https://console.cloud.google.com/earth-engine/welcome](https://console.cloud.google.com/earth-engine/welcome)

## Setup

Open a terminal on your machine with administrator privileges.

OPTIONAL: Open a Cloud Shell:  
https://console.cloud.google.com/cloudshelleditor?\&project=YOUR-PROJECT\&cloudshell=true 

OPTIONAL: Install virtualenv. Make a virtual environment and go into it:  
source ee-adk-env/bin/activate

Install Earth Engine and ADK:  
pip3 install \-U google-adk  
pip3 install earthengine-api

Make a folder for your agent and go into it.  
mkdir ee\_agent  
cd ee\_agent

Download some files from the `earthengine-community` Github repository using curl.  
curl \-o .env [https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk\_agents/biomass\_estimation\_agent/.env](https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk_agents/biomass_estimation_agent/.env)

Edit the .env file to add your Google Cloud Project information.  
nano .env

Download the rest of the agent files:  
curl \-o agent.py [https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk\_agents/biomass\_estimation\_agent/agent.py](https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk_agents/biomass_estimation_agent/agent.py) 

curl \-o prompts.py [https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk\_agents/biomass\_estimation\_agent/prompts.py](https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk_agents/biomass_estimation_agent/prompts.py) 

curl \-o tools.py [https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk\_agents/biomass\_estimation\_agent/tools.py](https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk_agents/biomass_estimation_agent/tools.py) 

curl \-o \_\_init\_\_.py [https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk\_agents/biomass\_estimation\_agent/\_\_init\_\_.py](https://raw.githubusercontent.com/google/earthengine-community/refs/heads/master/examples/adk_agents/biomass_estimation_agent/__init__.py)

## Authentication

Authenticate to Google Cloud or Earth Engine, if needed.  If you are in a Cloud Shell, you should not need to do this.

gcloud auth application-default login

Or

earthengine authenticate \--scopes https://www.googleapis.com/auth/earthengine,https://www.googleapis.com/auth/cloud-platform

## Run the ADK server

Go to the parent directory of the agent and run the ADK web server:  
cd ..  
adk web

Go to [http://127.0.0.1:8000/dev-ui/](http://127.0.0.1:8000/dev-ui/) or whatever address is printed by the previous command.

If you are in a Cloud Shell,  
adk web \--allow\_origins "regex:https://.\*\\.cloudshell\\.dev"

Then go to the Web Preview button in the upper right of the Cloud Shell.

## Example interaction

It's nice to ask your bot: "What can you do?" This will initialize the relevant libraries and load the agent's tools. This can be a useful debugging step.  If the bot itself doesn't tell you about any errors, you can inspect the console.

### Give a polygon to the agent

Ask the agent about a polygon. Use [this script](https://code.earthengine.google.com/597147bdaa381b403b286690f8f597a5) to get a not-too-big polygon. Copy it to the agent chat: 

"Please tell me about {"type":"Polygon","coordinates":\[\[\[-122.29065062984834,37.24640452631664\],\[-122.30163695797334,37.2361551304299\],\[-122.28052260860811,37.2306882160823\],\[-122.26610305294405,37.24408145207234\]\]\],"evenOdd":true}"

You don't have to say "please," but it's more polite. 

### Agent response:

This assessment analyzes the provided GeoJSON polygon, approximately 314.37 hectares (3.14 km²) in size, based on current remote sensing and geospatial data.

---

### **Executive Summary**

This area is a **High-Biomass Forest** demonstrating exceptionally high carbon density (\~383 Mg/ha). While the site historically showed a slight negative trend in biomass, recent high-resolution data (2020–2024) indicates a strong **carbon sequestration trajectory** despite experiencing a moderate disturbance event in 2021\. The land cover is highly stable and dominated by persistent tree cover.

---

#### **1\. Site Characterization & Land Use Identity**

| Metric | Value | Interpretation |
| :---- | :---- | :---- |
| **Total Area** | 3.14 km² (314.37 ha) | A medium-sized forest patch. |
| **Persistent Forest (2020)** | 81.7% (2.57 km²) | High stability, indicating that the majority of the area maintained tree cover for a long period. |
| **Natural Forest (2020)** | 37.3% (1.17 km²) | While a large portion is persistent forest, only a fraction is classified as "natural." This suggests the site is either a **managed forest** (e.g., timber production) or a long-established secondary forest recovering from past human activity. |
| **Dominant Land Cover (2020-2024)** | 100% Tree persistence | Dynamic World analysis shows perfect stability: all areas classified as Trees in 2020 remained Trees in 2024\. |

**Inference:** The data points to a **High-Biomass, Highly Stable Forest** area, likely a well-established secondary forest or commercial timber plantation managed for long-rotation harvest, given the discrepancy between high persistence and moderate natural forest classification.

#### **2\. Carbon & Biomass Stocking**

This area is extremely rich in Above Ground Biomass (AGB), indicating a dense forest ecosystem.

* **ESA Biomass Baseline (2022):** The long-term, coarser ESA dataset estimates the AGB stock at **185.74 Mg/ha**.  
* **High-Resolution Estimated Biomass (2020):** Using GEDI/Alpha Earth-tuned models, the AGB stock in 2020 was estimated at **351.47 Mg/ha**.  
* **Current Estimated Biomass (2024):** The current AGB stock is estimated at **382.90 Mg/ha**.

**Interpretation:** The most recent, high-resolution estimates place this site firmly in the category of a **High-Carbon Forest**, with an AGB density exceeding 380 Mg/ha. This value is characteristic of mature, dense forest ecosystems. The significant difference between the ESA and estimated data highlights the value of the GEDI-calibrated products for local assessment.

#### **3\. Trajectory & Trends**

The trend analysis presents a mixed, but ultimately positive, picture regarding recent growth.

* **Long-Term Trend (ESA Slope, 2007-2022):** **\-0.78 Mg/ha/year**. This negative slope indicates that, over the 15-year period covered by ESA data, the area was a net carbon source, likely due to small-scale degradation or harvesting events spread over time.  
* **Short-Term Trend (Estimated Biomass, 2020-2024):** The biomass increased from 351.47 Mg/ha to 382.90 Mg/ha. This represents a net gain of **\~31.43 Mg/ha over four years**, or an average annual sequestration rate of **\~7.86 Mg/ha/year**.

**Interpretation:** The site has reversed its long-term negative trend and is currently operating as a strong **carbon sink**, rapidly sequestering biomass between 2020 and 2024\.

#### **4\. Disturbance & Change Dynamics**

While the land cover remained stable (100% Trees \-\> Trees), the high-resolution change detection reveals a distinct disturbance history.

| Year | Change Area (m²) | Change Area (ha) | Notes |
| :---- | :---- | :---- | :---- |
| **2018-2019** | 0 | 0 | Stable |
| **2020** | 10,440.59 | 1.04 | Minor change detected |
| **2021** | **76,246.48** | **7.62** | **Significant Disturbance Event** |
| **2022-2025** | 0 | 0 | Stable |

**Analysis of the 2021 Event:** The **2021 spike** detected a significant loss of forest integrity across **7.62 hectares (2.42% of the total area)**. Since the Dynamic World transition matrix showed no conversion away from the "Trees" class, this was highly likely a **selective logging operation** or a **low-severity fire/degradation event**, rather than a clear-cut or conversion to agriculture.

The rapid increase in biomass between 2020 and 2024 indicates that the area is exhibiting strong resilience and recovery from this 2021 disturbance, absorbing the small loss of 2021 into the overall positive growth trend.

---

### **Sustainability Assessment**

Based on the evidence, the site's current status is highly positive from a carbon perspective:

* **Status:** **Strong Carbon Sink** (Gaining \~7.86 Mg/ha/year).  
* **Resilience:** High. The forest structure (Trees) remained intact after a significant 2021 disturbance, and the area quickly returned to a high growth trajectory.  
* **Risk:** The long-term negative trend suggests the potential for future degradation or periodic harvesting. The 2021 event is a prime example of this ongoing management or disturbance risk.

**Conclusion:** The area maintains a vast store of carbon and is actively sequestering more. Management appears focused on maintaining forest cover, though periodic (likely selective) disturbances occur. The data supports the conclusion that this is a healthy, rapidly growing, and carbon-rich forest ecosystem.

## Disclaimer

This is experimental. Use the agent with extreme caution.

## Clean-up

Disable all APIs on your Cloud Project to avoid future consumption fees.

## Resources

* ADK docs: [https://adk.dev/](https://adk.dev/)   
* API docs: [https://adk.dev/api-reference/python/index.html](https://adk.dev/api-reference/python/index.html)   
* Github samples: [https://adk.dev/api-reference/python/index.html](https://adk.dev/api-reference/python/index.html)  
* Agent skills: [https://agentskills.io/home](https://agentskills.io/home)  
* Earth Engine experimental ADK examples: [https://github.com/google/earthengine-community/tree/master/examples/adk\_agents](https://github.com/google/earthengine-community/tree/master/examples/adk_agents) 

## Model

From [https://adk.dev/agents/models/google-gemini/](https://adk.dev/agents/models/google-gemini/),: 

"Most code examples in ADK documentation use `gemini-flash-latest` to select the [latest available](https://ai.google.dev/gemini-api/docs/models#latest) Gemini Flash version. However, if you access Gemini from a regional endpoint, such as `us-central1`, this selection string may not work. In that case, use a specific model version string from the [Gemini models](https://ai.google.dev/gemini-api/docs/models) page or Google Cloud [Gemini models](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/models) list."