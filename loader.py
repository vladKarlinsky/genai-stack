import os
import requests
from dotenv import load_dotenv
from langchain_community.graphs import Neo4jGraph
import streamlit as st
from streamlit.logger import get_logger
from chains import load_embedding_model
from utils import create_constraints, create_vector_index
from PIL import Image
from PyPDF2 import PdfReader
import io

load_dotenv(".env")

url = os.getenv("NEO4J_URI")
username = os.getenv("NEO4J_USERNAME")
password = os.getenv("NEO4J_PASSWORD")
ollama_base_url = os.getenv("OLLAMA_BASE_URL")
embedding_model_name = os.getenv("EMBEDDING_MODEL")
# Remapping for Langchain Neo4j integration
os.environ["NEO4J_URL"] = url

logger = get_logger(__name__)

so_api_base_url = "https://api.stackexchange.com/2.3/search/advanced"

embeddings, dimension = load_embedding_model(
    embedding_model_name, config={"ollama_base_url": ollama_base_url}, logger=logger
)

# if Neo4j is local, you can go to http://localhost:7474/ to browse the database
neo4j_graph = Neo4jGraph(url=url, username=username, password=password)

create_constraints(neo4j_graph)
create_vector_index(neo4j_graph, dimension)


def load_so_data(tag: str = "neo4j", page: int = 1) -> None:
    parameters = (
        f"?pagesize=100&page={page}&order=desc&sort=creation&answers=1&tagged={tag}"
        "&site=stackoverflow&filter=!*236eb_eL9rai)MOSNZ-6D3Q6ZKb0buI*IVotWaTb"
    )
    data = requests.get(so_api_base_url + parameters).json()
    insert_so_data(data)


def load_high_score_so_data() -> None:
    parameters = (
        f"?fromdate=1664150400&order=desc&sort=votes&site=stackoverflow&"
        "filter=!.DK56VBPooplF.)bWW5iOX32Fh1lcCkw1b_Y6Zkb7YD8.ZMhrR5.FRRsR6Z1uK8*Z5wPaONvyII"
    )
    data = requests.get(so_api_base_url + parameters).json()
    insert_so_data(data)

def fetch_data(url):
    response = requests.get(url)
    if response.status_code == 200:
        return response.json()
    else:
        return None

def fetch_israeli_laws(law_id):
    url = f"https://knesset.gov.il/Odata/ParliamentInfo.svc/KNS_IsraelLaw({law_id})?$format=json&$expand=KNS_IsraelLawNames,KNS_LawBindings,KNS_IsraelLawMinsitries,KNS_IsraelLawClassificiations"
    law_data = fetch_data(url)
    return law_data

def fetch_law_details(law_id): 
    # Fetch data first from KNS_Bill entity
    url = f"https://knesset.gov.il/Odata/ParliamentInfo.svc/KNS_Bill({law_id})?$format=json"
    law_data_from_KNS_bill = fetch_data(url)
    if law_data_from_KNS_bill:
        return law_data_from_KNS_bill
    # If no data, try KNS_Law entity
    url = f"https://knesset.gov.il/Odata/ParliamentInfo.svc/KNS_Law({law_id})?$format=json"
    law_data_from_KNS_law = fetch_data(url)
    if law_data_from_KNS_law:
        return law_data_from_KNS_law
    # Return default value
    return "No data"


def fetch_pdf_link_from_bill(law_id):
    # Fetch data first from Bill entity
    url = f"https://knesset.gov.il/Odata/ParliamentInfo.svc/KNS_DocumentBill?$format=json&$filter=BillID%20eq%20{law_id}"
    law_data_from_KNS_DocumentBill = fetch_data(url)
    if law_data_from_KNS_DocumentBill:
        for doc in law_data_from_KNS_DocumentBill["value"]:
            if doc["ApplicationDesc"] == "PDF":
                return doc["FilePath"]
    # If no data, try Law entity
    url = f"https://knesset.gov.il/Odata/ParliamentInfo.svc/KNS_DocumentLaw?$format=json&$filter=LawID%20eq%20{law_id}"
    law_data_from_KNS_DocumentLaw = fetch_data(url)
    if law_data_from_KNS_DocumentLaw:
        for doc in law_data_from_KNS_DocumentLaw["value"]:
            if doc["ApplicationDesc"] == "PDF":
                return doc["FilePath"]
    # Return default value
    return "No link"

def fetch_pdf_text_from_bill(law_id):
    file_path_url = fetch_pdf_link_from_bill(law_id)
    if file_path_url != "No link":
        response = requests.get(file_path_url)
        pdf_file = io.BytesIO(response.content)
        reader = PdfReader(pdf_file)
        text = []
        for page in reader.pages:
            text.append(page.extract_text())
        return "\n".join(text)
    return "No text or info"

def process_law_data(law_id):
    url = f"https://knesset.gov.il/Odata/ParliamentInfo.svc/KNS_IsraelLaw({law_id})?$format=json&$expand=KNS_IsraelLawNames,KNS_LawBindings,KNS_IsraelLawMinsitries,KNS_IsraelLawClassificiations"
    law_data = fetch_data(url)
    
    if law_data:
        # Create Law Node
        law_properties = {
            'names':', '.join([name['Name'] for name in law_data['KNS_IsraelLawNames']]),
            'publication_date': law_data['PublicationDate'],
            'latest_date': law_data['LatestPublicationDate'],
            'classifications': [cls['ClassificiationDesc'] for cls in law_data['KNS_IsraelLawClassificiations']],
            'validity': law_data['LawValidityDesc'],
            'law_id': law_id,
            'link': f"https://main.knesset.gov.il/activity/legislation/laws/pages/LawPrimary.aspx?t=lawlaws&st=lawlaws&lawitemid={law_id}"
        }

        neo4j_graph.query("""
                CREATE (l:Law {
                    names: $names,
                    publication_date: $publication_date,
                    latest_date: $latest_date,
                    classifications: $classifications,
                    validity: $validity,
                    law_id: $law_id,
                    link: $link
                })
            """, law_properties)
        
        # Process each binding as an Amendment Node
        for binding in law_data['KNS_LawBindings']:
            law_details = fetch_law_details(binding['LawID'])
            amendment_properties = {
                'name': law_details["Name"] if isinstance(law_details, dict) and "Name" in law_details else law_details,
                'type': binding['BindingTypeDesc'],
                'publication_date': law_details["PublicationDate"] if isinstance(law_details, dict) and "PublicationDate" in law_details else law_details,
                'law_id': binding['LawID'],
                'link': fetch_pdf_link_from_bill(binding['LawID']),
                'text': fetch_pdf_text_from_bill(binding['LawID'])
            }
            neo4j_graph.query("""
                    MERGE (a:Amendment {
                        name: $name,
                        type: $type,
                        publication_date: $publication_date,
                        law_id: $law_id,
                        link: $link,
                        text: $text
                    })
                    WITH a
                    MATCH (l:Law {law_id: $parent_law_id})
                    MERGE (a)-[r:AMENDS {date: $publication_date}]->(l)
                """, {**amendment_properties, 'parent_law_id': law_id})


def insert_so_data(data: dict) -> None:
    # Calculate embedding values for questions and answers
    for q in data["items"]:
        question_text = q["title"] + "\n" + q["body_markdown"]
        q["embedding"] = embeddings.embed_query(question_text)
        for a in q["answers"]:
            a["embedding"] = embeddings.embed_query(
                question_text + "\n" + a["body_markdown"]
            )

    # Cypher, the query language of Neo4j, is used to import the data
    # https://neo4j.com/docs/getting-started/cypher-intro/
    # https://neo4j.com/docs/cypher-cheat-sheet/5/auradb-enterprise/
    import_query = """
    UNWIND $data AS q
    MERGE (question:Question {id:q.question_id}) 
    ON CREATE SET question.title = q.title, question.link = q.link, question.score = q.score,
        question.favorite_count = q.favorite_count, question.creation_date = datetime({epochSeconds: q.creation_date}),
        question.body = q.body_markdown, question.embedding = q.embedding
    FOREACH (tagName IN q.tags | 
        MERGE (tag:Tag {name:tagName}) 
        MERGE (question)-[:TAGGED]->(tag)
    )
    FOREACH (a IN q.answers |
        MERGE (question)<-[:ANSWERS]-(answer:Answer {id:a.answer_id})
        SET answer.is_accepted = a.is_accepted,
            answer.score = a.score,
            answer.creation_date = datetime({epochSeconds:a.creation_date}),
            answer.body = a.body_markdown,
            answer.embedding = a.embedding
        MERGE (answerer:User {id:coalesce(a.owner.user_id, "deleted")}) 
        ON CREATE SET answerer.display_name = a.owner.display_name,
                      answerer.reputation= a.owner.reputation
        MERGE (answer)<-[:PROVIDED]-(answerer)
    )
    WITH * WHERE NOT q.owner.user_id IS NULL
    MERGE (owner:User {id:q.owner.user_id})
    ON CREATE SET owner.display_name = q.owner.display_name,
                  owner.reputation = q.owner.reputation
    MERGE (owner)-[:ASKED]->(question)
    """
    neo4j_graph.query(import_query, {"data": data["items"]})


# Streamlit
def get_tag() -> str:
    input_text = st.text_input(
        "Which tag questions do you want to import?", value="neo4j"
    )
    return input_text


def get_pages():
    col1, col2 = st.columns(2)
    with col1:
        num_pages = st.number_input(
            "Number of pages (100 questions per page)", step=1, min_value=1
        )
    with col2:
        start_page = st.number_input("Start page", step=1, min_value=1)
    st.caption("Only questions with answers will be imported.")
    return (int(num_pages), int(start_page))


def render_page():
    datamodel_image = Image.open("./images/datamodel.png")
    st.header("StackOverflow Loader")
    st.subheader("Choose StackOverflow tags to load into Neo4j")
    st.caption("Go to http://localhost:7474/ to explore the graph.")

    user_input = get_tag()
    num_pages, start_page = get_pages()

    if st.button("Import", type="primary"):
        with st.spinner("Loading... This might take a minute or two."):
            try:
                # for page in range(1, num_pages + 1):
                #     load_so_data(user_input, start_page + (page - 1))
                law_ids = list(range(2000001, 2000011))
                for law_id in law_ids:
                    process_law_data(law_id) # Execution time: 8:45 mins
                st.success("Import successful", icon="✅")
                st.caption("Data model")
                st.image(datamodel_image)
                st.caption("Go to http://localhost:7474/ to interact with the database")
            except Exception as e:
                st.error(f"Error: {e}", icon="🚨")
    with st.expander("Highly ranked questions rather than tags?"):
        if st.button("Import highly ranked questions"):
            with st.spinner("Loading... This might take a minute or two."):
                try:
                    load_high_score_so_data()
                    st.success("Import successful", icon="✅")
                except Exception as e:
                    st.error(f"Error: {e}", icon="🚨")


render_page()
