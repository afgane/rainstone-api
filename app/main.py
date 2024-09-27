import pandas as pd
from fastapi import FastAPI, Query, Path, UploadFile, File, HTTPException
from pydantic import BaseModel
from typing import List
import json
import os

from fastapi.middleware.cors import CORSMiddleware

# Get the root path from an environment variable, defaulting to "/api" if not set
ROOT_PATH = os.getenv("API_ROOT_PATH", "/api")

app = FastAPI(
    title="Rainstone",
    description="A cloud costs estimator for computational biology tools.",
    version="0.1.0",
    root_path=ROOT_PATH,
    root_path_in_servers=False,
    debug=True,
)
# Get the allowed origins from an environment variable
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173").split(",")
app.add_middleware(
    CORSMiddleware,
    # allow_origins=["*", "null"],
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load the CSV file with tool costs
df = pd.read_csv("static/tool_costs.csv")


# Models
class Tool(BaseModel):
    toolId: str
    averageCostPerJob: float
    medianCostPerJob: float
    p95CostPerJob: float
    averageJobRuntimeSec: int
    medianJobRuntimeSec: int
    p95JobRuntimeSec: int
    averageJobInputSizeMB: float
    medianJobInputSizeMB: float
    p95JobInputSizeMB: float
    numJobs: int
    averageAllocatedMemoryGB: float
    medianAllocatedMemoryGB: float
    p95AllocatedMemoryGB: float


class MissingTool(BaseModel):
    toolId: str


class CostEstimate(BaseModel):
    avgCostEst: float
    medianCostEst: float
    p95CostEst: float
    tools: List[Tool]
    missingTools: List[MissingTool]


def csv_row_to_tool(row):
    return Tool(
        toolId=row["tool_id"],
        averageCostPerJob=row["avg_job_cost"],
        medianCostPerJob=row["median_job_cost"],
        p95CostPerJob=row["p95_job_cost"],
        averageJobRuntimeSec=int(row["avg_runtime_seconds"]),
        medianJobRuntimeSec=int(row["median_runtime_seconds"]),
        p95JobRuntimeSec=int(row["p95_runtime_seconds"]),
        averageJobInputSizeMB=row["avg_input_size_mb"],
        medianJobInputSizeMB=row["median_input_size_mb"],
        p95JobInputSizeMB=row["p95_input_size_mb"],
        numJobs=int(row["num_jobs"]),
        averageAllocatedMemoryGB=row["avg_allocated_memory_gb"],
        medianAllocatedMemoryGB=row["median_allocated_memory_gb"],
        p95AllocatedMemoryGB=row["p95_allocated_memory_gb"],
    )


# Routes
@app.get("/")
async def root():
    return {
        "message": "Welcome to the Rainstone - a cloud cost estimator for bioinformatics! Head to /docs to get started."
    }


@app.get("/tools", response_model=List[Tool])
async def list_tools(
    skip: int = Query(0, description="Number of records to skip for pagination."),
    limit: int = Query(500, description="Maximum number of records to return.", le=500),
    sort_by: str = Query("averageCostPerJob", description="Field to sort by"),
    sort_order: str = Query("asc", description="Sort order (asc or desc)"),
):
    # Map the sort_by parameter to the corresponding column name in the DataFrame
    sort_column_mapping = {
        "averageCostPerJob": "avg_job_cost",
        "medianCostPerJob": "median_job_cost",
        "p95CostPerJob": "p95_job_cost",
    }

    sort_column = sort_column_mapping.get(sort_by, "avg_job_cost")

    # Sort the DataFrame
    sorted_df = df.sort_values(by=sort_column, ascending=(sort_order.lower() == "asc"))

    # Apply pagination
    paginated_df = sorted_df.iloc[skip : skip + limit]

    # Convert to Tool objects
    tools = paginated_df.apply(csv_row_to_tool, axis=1).tolist()

    return tools


@app.get("/tools/{toolId}", response_model=Tool)
async def get_tool(
    toolId: str = Path(..., description="Id of a tool, excluding version or author")
):
    tool_data = df[df["tool_id"] == toolId]
    if tool_data.empty:
        raise HTTPException(status_code=404, detail="Tool not found")
    return csv_row_to_tool(tool_data.iloc[0])


@app.post("/workflow", response_model=CostEstimate)
async def process_workflow(galaxyWorkflow: UploadFile = File(...)):
    try:
        workflow_content = await galaxyWorkflow.read()
        workflow = json.loads(workflow_content)
        # Parse the workflow and extract tool IDs
        # Iterate through the workflow steps to find the tool_shed_repository and extract the tool
        wf_tools = []
        missing_tools = []
        for step_id, step_data in workflow.get("steps", {}).items():
            tool_id = step_data.get("tool_id")
            if tool_id:
                # Split the tool_id by '/' and extract the second-to-last part
                tool_id_parts = tool_id.split("/")
                if len(tool_id_parts) > 1:
                    tool_id = tool_id_parts[-2].lower()
                    tool_data = df[df["tool_id"] == tool_id]
                else:
                    tool_id = tool_id_parts[0].lower()
                    tool_data = df[df["tool_id"] == tool_id]
                if tool_data.empty:
                    missing_tools.append(MissingTool(toolId=tool_id))
                else:
                    wf_tools.append(csv_row_to_tool(tool_data.iloc[0]))

        return CostEstimate(
            avgCostEst=sum(tool.averageCostPerJob for tool in wf_tools),
            medianCostEst=sum(tool.medianCostPerJob for tool in wf_tools),
            p95CostEst=sum(tool.p95CostPerJob for tool in wf_tools),
            tools=wf_tools,
            missingTools=missing_tools,
        )
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid workflow file")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
