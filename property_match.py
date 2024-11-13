# pip install qdrant-client sentence-transformers numpy Pillow requests scikit-learn

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from time import sleep
from typing import List, Dict, Optional, Tuple

import numpy as np
import requests
from io import BytesIO
from PIL import Image
from pydantic import UUID1
from qdrant_client import QdrantClient, models
from sentence_transformers import SentenceTransformer
from sklearn.preprocessing import normalize

# Set up logging configuration
logging.basicConfig(level=logging.INFO)

class SearchMode(Enum):
    """
    Enumeration of different search modes with varying weights
    for different property attributes.
    """
    BALANCED = "balanced"
    VISUAL_FOCUS = "visual_focus"
    FEATURES_FOCUS = "features_focus"
    LOCATION_FOCUS = "location_focus"

@dataclass
class PropertyFilters:
    """
    Dataclass representing filters that can be applied to property search results.
    """
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    min_bedrooms: Optional[int] = None
    max_bedrooms: Optional[int] = None
    min_bathrooms: Optional[int] = None
    max_bathrooms: Optional[int] = None
    property_type: Optional[str] = None
    must_have_amenities: List[str] = field(default_factory=list)

class PropertyData:
    """
    Handles property data and embedding generation with proper normalization.
    """

    def __init__(self):
        # Initialize text and image embedding models
        self.text_model = SentenceTransformer('all-MiniLM-L6-v2')
        self.image_model = SentenceTransformer('clip-ViT-B-32')

    def preprocess_text(self, text: str) -> str:
        """
        Normalize and clean text data.

        Args:
            text (str): The text to preprocess.

        Returns:
            str: The preprocessed text.
        """
        return text.lower().strip()

    def generate_text_embeddings(self, property_data: Dict) -> Dict[str, np.ndarray]:
        """
        Generate normalized text embeddings for different property attributes.

        Args:
            property_data (Dict): The property data.

        Returns:
            Dict[str, np.ndarray]: A dictionary of normalized embeddings.
        """
        # Prepare text data for embeddings
        location_text = self.preprocess_text(
            f"{property_data.get('location_description', '')} "
            f"{property_data.get('neighborhood', '')} "
            f"{property_data.get('city', '')} "
            f"{property_data.get('municipality', '')} "
            f"{property_data.get('county', '')}"
        )

        property_features_text = self.preprocess_text(" ".join([
            *property_data.get('amenities', []),
            *property_data.get('interior_features', []),
            *property_data.get('appliances', []),
            *property_data.get('exterior_features', []),
            *property_data.get('lot_features', []),
            f"property_type: {property_data.get('property_type', 'not specified')}",
            f"architectural_style: {property_data.get('architectural_style', 'not specified')}"
        ]))

        # Generate embeddings
        embeddings = {
            "location": self.text_model.encode(location_text),
            "features": self.text_model.encode(property_features_text)
            # 'policies' embedding removed
        }

        # Normalize embeddings
        for key in embeddings:
            embeddings[key] = normalize(embeddings[key].reshape(1, -1))[0]

        return embeddings

    def generate_image_embedding(self, image_urls: List[str]) -> Optional[np.ndarray]:
        """
        Generate a normalized aggregated image embedding from property photos.

        Args:
            image_urls (List[str]): A list of image URLs.

        Returns:
            Optional[np.ndarray]: The aggregated image embedding, or None if failed.
        """
        embeddings = []
        for url in image_urls[:5]:  # Limit to first 5 images
            try:
                # Download image
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                img = Image.open(BytesIO(response.content)).convert('RGB')

                # Generate embedding
                embedding = self.image_model.encode(img)
                embedding = normalize(embedding.reshape(1, -1))[0]
                embeddings.append(embedding)
            except requests.exceptions.RequestException as e:
                logging.warning(f"Error fetching image {url}: {e}")
                continue
            except Exception as e:
                logging.warning(f"Error processing image {url}: {e}")
                continue

        if not embeddings:
            return None

        # Aggregate embeddings by computing the mean
        mean_embedding = np.mean(embeddings, axis=0)
        mean_embedding = normalize(mean_embedding.reshape(1, -1))[0]
        return mean_embedding

class PropertyIndexer:
    """
    Handles indexing of property data into Qdrant collections.
    """

    def __init__(self, client: QdrantClient):
        self.client = client
        self.property_data = PropertyData()

    def validate_property_data(self, property_data: Dict) -> bool:
        """
        Validate required fields and data formats in property data.

        Args:
            property_data (Dict): The property data to validate.

        Returns:
            bool: True if validation passes, False otherwise.
        """
        required_fields = ["id", "location_description", "amenities", "photo_urls"]
        for field in required_fields:
            if field not in property_data:
                logging.error(f"Missing required field: {field}")
                return False
        return True

    def index_property(self, property_data: Dict) -> bool:
        """
        Index a property with normalized vectors into Qdrant collections.

        Args:
            property_data (Dict): The property data to index.

        Returns:
            bool: True if indexing is successful, False otherwise.
        """
        try:
            if not self.validate_property_data(property_data):
                raise ValueError("Property data validation failed")

            # Generate embeddings
            text_embeddings = self.property_data.generate_text_embeddings(property_data)
            image_embedding = self.property_data.generate_image_embedding(property_data["photo_urls"])

            if image_embedding is None:
                raise ValueError("Failed to generate image embeddings")

            # Store normalized vectors in their respective collections
            collections = [
                "location_vectors",
                "features_vectors",
                "visual_vectors"
            ]
            vectors = [
                text_embeddings["location"],
                text_embeddings["features"],
                image_embedding
            ]

            for collection, vector in zip(collections, vectors):
                response = self.client.upsert(
                    collection_name=collection,
                    points=[{
                        "id": property_data["id"],
                        "vector": [0.1, 0.2, 0.3, 0.4, 0.5],
                        "payload": property_data
                    }]
                )
                print(f"Vector type: {type(vector[0])}")
                logging.info(f"Upsert response for collection {collection}: {response}")

            logging.info(f"Successfully indexed property {property_data['id']}")
            return True

        except Exception as e:
            logging.error(f"Error indexing property {property_data.get('id', 'unknown')}: {e}")
            return False

class PropertySearcher:
    """
    Handles multi-collection search and result aggregation.
    """

    def __init__(self, client: QdrantClient):
        self.client = client
        # Define weights for each search mode
        self.search_modes = {
            SearchMode.BALANCED.value: {
                "location": 0.4,
                "features": 0.4,
                "visual": 0.2
            },
            SearchMode.VISUAL_FOCUS.value: {
                "location": 0.1,
                "features": 0.1,
                "visual": 0.8
            },
            SearchMode.FEATURES_FOCUS.value: {
                "location": 0.1,
                "features": 0.8,
                "visual": 0.1
            },
            SearchMode.LOCATION_FOCUS.value: {
                "location": 0.8,
                "features": 0.1,
                "visual": 0.1
            }
        }

    def apply_filters(self, properties: List[Dict], filters: PropertyFilters) -> List[Dict]:
        """
        Apply post-search filters to the list of properties.

        Args:
            properties (List[Dict]): The list of property data to filter.
            filters (PropertyFilters): The filters to apply.

        Returns:
            List[Dict]: The filtered list of properties.
        """
        if not filters:
            return properties

        filtered_results = []
        for prop in properties:
            try:
                # Price filtering
                if 'price_range' in prop:
                    price_min, price_max = map(float, prop['price_range'].split('-'))
                    if filters.min_price is not None and price_max < filters.min_price:
                        continue
                    if filters.max_price is not None and price_min > filters.max_price:
                        continue
                else:
                    continue  # Exclude properties without price information

                # Bedroom filtering
                bedrooms = prop.get('bedrooms', None)
                if bedrooms is not None:
                    if filters.min_bedrooms is not None and bedrooms < filters.min_bedrooms:
                        continue
                    if filters.max_bedrooms is not None and bedrooms > filters.max_bedrooms:
                        continue
                else:
                    continue  # Exclude properties without bedroom information

                # Bathroom filtering
                bathrooms = prop.get('bathrooms', None)
                if bathrooms is not None:
                    if filters.min_bathrooms is not None and bathrooms < filters.min_bathrooms:
                        continue
                    if filters.max_bathrooms is not None and bathrooms > filters.max_bathrooms:
                        continue
                else:
                    continue  # Exclude properties without bathroom information

                # Property type filtering
                if filters.property_type is not None and prop.get('property_type') != filters.property_type:
                    continue

                # Amenities filtering
                if filters.must_have_amenities:
                    if not all(amenity in prop.get('amenities', []) for amenity in filters.must_have_amenities):
                        continue

                filtered_results.append(prop)
            except Exception as e:
                logging.warning(f"Error applying filters to property {prop.get('id')}: {e}")
                continue

        return filtered_results

    def search_similar_properties(
            self,
            property_id: int,
            mode: SearchMode = SearchMode.BALANCED,
            filters: Optional[PropertyFilters] = None,
            top_k: int = 10
    ) -> List[Dict]:
        """
        Search for properties similar to the given property ID.

        Args:
            property_id (int): The ID of the property to find similarities for.
            mode (SearchMode): The search mode determining attribute weights.
            filters (Optional[PropertyFilters]): Filters to apply to the results.
            top_k (int): The number of top results to return.

        Returns:
            List[Dict]: A list of similar property data.
        """
        try:
            # Fetch the weights for the chosen search mode
            weights = self.search_modes[mode.value]

            # Initialize a dictionary to store search results
            search_results = {}

            # Iterate through each vector collection (e.g., location, features, visual)
            for key in ["location", "features", "visual"]:
                collection = f"{key}_vectors"

                # Retrieve the vector for the property ID
                initial_vector_result = self.client.retrieve(
                    collection_name=collection,
                    ids=[property_id]
                )

                # Ensure we retrieved a valid vector for the property ID
                if not initial_vector_result or not initial_vector_result[0].vector:
                    raise ValueError(f"Property ID {property_id} not found in {collection}")

                # Extract the vector for similarity search
                vector = initial_vector_result[0].vector

                # Perform similarity search using query_points
                results = self.client.query_points(
                    collection_name=collection,
                    vector=vector,
                    top=top_k * 2  # Fetch extra results to improve merging quality
                )
                search_results[key] = results

            # Merge results using weighted Reciprocal Rank Fusion
            merged_results = self._weighted_rrf_merge(search_results, weights)

            # Retrieve full property data, exclude the original property, and apply filters
            properties = []
            for prop_id, _ in merged_results[:top_k * 5]:
                point = self.client.retrieve(
                    collection_name="location_vectors",
                    ids=[prop_id]
                )
                if point and point[0].payload:
                    if prop_id != property_id:  # Exclude the query property itself
                        properties.append(point[0].payload)

            # Apply filters to the properties
            filtered_results = self.apply_filters(properties, filters)
            return filtered_results[:top_k]

        except Exception as e:
            logging.error(f"Error searching for similar properties: {e}")
            return []

    def _weighted_rrf_merge(
        self,
        search_results: Dict[str, List[models.ScoredPoint]],
        weights: Dict[str, float],
        k: int = 60
    ) -> List[Tuple[str, float]]:
        """
        Merge search results from different collections using Weighted Reciprocal Rank Fusion.

        Args:
            search_results (Dict[str, List[models.ScoredPoint]]): Search results from each collection.
            weights (Dict[str, float]): Weights for each collection.
            k (int): The constant used in the RRF formula.

        Returns:
            List[Tuple[str, float]]: A list of property IDs and their aggregated scores.
        """
        scores = {}
        for key, results in search_results.items():
            weight = weights.get(key, 0)
            for rank, result in enumerate(results):
                property_id = result.id
                # Compute the weighted RRF score
                scores[property_id] = scores.get(property_id, 0) + weight * (1 / (k + rank + 1))

        # Sort properties by their aggregated scores in descending order
        sorted_properties = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return sorted_properties

    def print_collection_data(self, collection_name: str, limit: int = 10):
        """
        Print data from a Qdrant collection for inspection.

        Args:
            collection_name (str): The name of the collection to inspect.
            limit (int): The number of points to retrieve in each scroll batch.
        """
        sleep(1)
        try:
            # Initialize the scroll without with_vector (only with payload and IDs)
            points, next_page_offset = self.client.scroll(
                collection_name=collection_name,
                limit=limit,
                with_payload=True
            )

            # Keep scrolling until no more points are returned
            while points:
                # Extract IDs from the points retrieved
                point_ids = [point.id for point in points]

                collection_info = self.client.get_collection(collection_name)
                logging.debug(f"Collection info: {collection_info}")
                # collection_description = self.client.describe_collection(collection_name)
                # logging.debug(f"Collection description: {collection_description}")

                # Retrieve the full data (including vectors) for each point ID
                full_points = self.client.retrieve(
                    collection_name=collection_name,
                    ids=point_ids
                )

                for full_point in full_points:
                    print(f"ID: {full_point.id}")
                    print(f"Vector: {full_point.vector}")
                    print(f"Payload: {full_point.payload}")
                    print("-" * 40)  # Separator for readability

                # Continue scrolling if there is a next page offset
                if next_page_offset is None:
                    break

                # Fetch the next set of points using the new offset
                points, next_page_offset = self.client.scroll(
                    collection_name=collection_name,
                    limit=limit,
                    with_payload=True,
                    offset=next_page_offset
                )

        except Exception as e:
            logging.error(f"Error retrieving data from collection {collection_name}: {e}")


def initialize_collections(client: QdrantClient):
    """
    Initialize Qdrant collections with proper vector configurations.

    Args:
        client (QdrantClient): The Qdrant client instance.
    """
    collections = {
        "location_vectors": 5,
        "features_vectors":5,
        "visual_vectors": 5
    }

    for collection in collections.keys():
        try:
            # Check if the collection already exists
            client.get_collection(collection_name=collection)
            logging.info(f"Collection '{collection}' already exists.")
        except Exception:
            # Create the collection if it doesn't exist
            client.create_collection(
                collection_name=collection,
                vectors_config=models.VectorParams(
                    size=collections[collection],  # Adjust based on your model's output dimension
                    distance=models.Distance.COSINE
                )
            )
            logging.info(f"Created collection '{collection}'.")
            sleep(1)
            collection_info = client.get_collection(collection)
            logging.debug(f"Collection info: {collection_info}")


# Example usage
if __name__ == "__main__":
    # Initialize Qdrant client
    client = QdrantClient(url="http://localhost:6333")

    # Initialize collections
    initialize_collections(client)

    # Initialize components
    indexer = PropertyIndexer(client)
    searcher = PropertySearcher(client)

    # Example property data
    example_property = {
        "id": 1,
        "location_description": "Downtown waterfront apartment with city views",
        "amenities": ["parking", "pool", "gym", "doorman"],
        "interior_features": ["hardwood floors", "granite countertops"],
        "appliances": ["refrigerator", "dishwasher", "oven"],
        "exterior_features": ["balcony", "rooftop terrace"],
        "lot_features": ["waterfront"],
        "architectural_style": "modern",
        "municipality": "Metro City",
        "county": "Metro County",
        "price_range": "2000-2500",
        "photo_urls": ["https://api-trestle.corelogic.com/trestle/Media/REBNY/Property/PHOTO-Jpeg/1090420583/18/MzM2Ny8yMTY3LzIw/MjAvMTAyNjUvMTczMTQ2MDU1NQ/apV_PyPXYs4Fe57EbKtFcpnGSgSLbVHDJzhSC-HNQZU"],  # Replace with actual image URLs
        "bedrooms": 2,
        "bathrooms": 2,
        "property_type": "apartment",
        "neighborhood": "Downtown",
        "city": "Metropolis"
    }

    # Index example property
    success = indexer.index_property(example_property)
    # success = True
    if success:
        # Index more properties for a meaningful search
        # TODO: Index multiple properties to populate the database

        searcher.print_collection_data('visual_vectors')

        # Define search filters
        filters = PropertyFilters(
            min_price=1800,
            max_price=3000,
            min_bedrooms=2,
            max_bedrooms=3,
            must_have_amenities=["parking", "pool"]
        )

        # Find similar properties
        similar_properties = searcher.search_similar_properties(
            property_id=1,
            mode=SearchMode.BALANCED,
            filters=filters,
            top_k=5
        )

        # Display results
        for prop in similar_properties:
            print(f"\nProperty ID: {prop['id']}")
            print(f"Location: {prop['location_description']}")
            print(f"Price: {prop['price_range']}")
            print(f"Amenities: {', '.join(prop.get('amenities', []))}")
            print(f"Bedrooms: {prop.get('bedrooms')}")
            print(f"Bathrooms: {prop.get('bathrooms')}")
            print(f"Interior Features: {', '.join(prop.get('interior_features', []))}")
            print(f"Appliances: {', '.join(prop.get('appliances', []))}")
            print(f"Exterior Features: {', '.join(prop.get('exterior_features', []))}")
            print(f"Lot Features: {', '.join(prop.get('lot_features', []))}")
            print(f"Architectural Style: {prop.get('architectural_style')}")
            print(f"Municipality: {prop.get('municipality')}")
            print(f"County: {prop.get('county')}")
    else:
        logging.error("Failed to index the example property.")