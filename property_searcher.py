import logging
from time import sleep
from typing import List, Dict, Optional, Tuple

from qdrant_client import QdrantClient, models

from common_class import SearchMode, PropertyFilters

# Set up logging configuration
logging.basicConfig(level=logging.INFO)

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
                if 'price_range' in prop and prop.get('price_range'):
                    price_min, price_max = map(float, prop['price_range'].split('-'))
                    if filters.min_price is not None and price_max < filters.min_price:
                        continue
                    if filters.max_price is not None and price_min > filters.max_price:
                        continue
                elif 'list_price' in prop and prop.get('list_price'):
                    if filters.min_price > prop.get('list_price') or filters.max_price < prop.get('list_price'):
                        continue
                else:
                    continue  # Exclude properties without price information

                # Bedroom filtering
                bedrooms = prop.get('bedrooms_total', None)
                if bedrooms is not None:
                    if filters.min_bedrooms is not None and bedrooms < filters.min_bedrooms:
                        continue
                    if filters.max_bedrooms is not None and bedrooms > filters.max_bedrooms:
                        continue
                else:
                    continue  # Exclude properties without bedroom information

                # Bathroom filtering
                bathrooms = prop.get('lp_calculated_bath', None)
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
                    if not all(amenity in prop.get('lp_listing_description', "") for amenity in filters.must_have_amenities):
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
                    ids=[property_id],
                    with_vectors=True
                )

                # Ensure we retrieved a valid vector for the property ID
                if not initial_vector_result or not initial_vector_result[0].vector:
                    raise ValueError(f"Property ID {property_id} not found in {collection}")

                # Extract the vector for similarity search
                vector = initial_vector_result[0].vector

                # Perform similarity search using query_points
                results = self.client.search(
                    collection_name=collection,
                    query_vector=vector,  # Replace 'vector' with 'query_vector'
                    limit=top_k * 2  # Replace 'top' with 'limit'
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
                logging.info(f"Collection info: {collection_info}")
                # collection_description = self.client.describe_collection(collection_name)
                # logging.debug(f"Collection description: {collection_description}")

                # Retrieve the full data (including vectors) for each point ID
                full_points = self.client.retrieve(
                    collection_name=collection_name,
                    ids=point_ids,
                    with_vectors=True
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
